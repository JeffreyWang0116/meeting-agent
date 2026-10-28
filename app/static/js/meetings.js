import { api } from "./api.js";
import { renderAskScope } from "./ask.js";
import { $, PAGE_SIZE, REFRESHERS, esc, icon, loadFail, pageNo, paginate, registerPager, registerRefresher, renderPager, showError, showNotice } from "./core.js";
import { renderHome } from "./home.js";
import { refreshReminders, remindersLoaded, renderReminders } from "./reminders.js";
import { copyWithFeedback, renderResult } from "./result.js";
import { correctTypos } from "./setup.js";
import { refreshTasks, renderTasks, tasksLoaded } from "./tasks.js";
import { speakerLabels, speakerNameProblem } from "./speakers.js";
import { TIME_RE, jumpToTranscript, renderChat } from "./transcript.js";

let meetingsLoaded = false;

/* ==================================================================
   6. 歷史會議：查閱、編輯、重新分析、分享、講者改名、刪除
   ================================================================== */
// ---- 歷史會議（查閱 / 編輯 / 重新分析 / 分享 / 講者改名 / 刪除） ----
let allMeetings = [];
let expandedMeetingId = null;      // 展開詳情中的會議
let detailEditing = false;
const meetingDetailCache = {};     // id -> 完整紀錄（含逐字稿）

// 任務庫、主動提醒都要把資料依會議分組，需要把 meeting_id 換成看得懂的標題。
// 集中在這裡，兩邊查同一份 allMeetings（ES module 的 live binding，讀到的永遠
// 是最新載入的清單）。查不到（會議還沒載完、或已被刪）回 null，由呼叫端決定
// 顯示什麼備援文字。
function meetingLabel(id) {
  const m = allMeetings.find(x => x.id === id);
  if (!m) return null;
  return { title: m.meeting?.title || "（未命名會議）", date: m.meeting?.date || "" };
}

// 講者改名清單：與逐字稿畫面同一套判斷，「重點：」這種行首不會被列進來
function detectSpeakers(text, knownNames = []) {
  const lines = String(text || "").split("\n").map(l => l.trim().replace(TIME_RE, ""));
  return [...speakerLabels(lines, knownNames)].slice(0, 12);
}

// 講者一律標代號（系統不猜姓名），由使用者點講者改名。問新名字並驗證；取消或不合格回 null
function askSpeakerName(oldName) {
  const input = prompt(`把「${oldName}」改名為：`, oldName);
  if (input === null) return null;
  const newName = input.trim();
  if (newName === oldName) return null;
  const problem = speakerNameProblem(newName);
  if (problem) { showError(problem); return null; }
  return newName;
}

// 改名：逐字稿講者欄、出席者、摘要、重點、決議、待確認、種類欄位，以及這場會議的任務
// （負責人、任務名稱、優先原因）。引用原句不動，否則跳不回逐字稿出處。回傳 { meeting, tasks }
async function renameSpeaker(id, oldName, newName) {
  const updated = await api.renameSpeaker(id, oldName, newName);
  meetingDetailCache[id] = updated.meeting;
  refreshTasks();
  refreshReminders();
  return updated;
}

function meetingDetailHtml(id) {
  const d = meetingDetailCache[id];
  if (!d) return `<div class="meeting-detail"><p class="empty-note">載入中…</p></div>`;
  if (detailEditing) {
    return `<div class="meeting-detail detail-edit">
        <label class="lbl">標題</label>
        <input type="text" id="dTitle" value="${esc(d.meeting.title)}">
        <label class="lbl">分類標籤（用「、」或逗號分隔，可自訂）</label>
        <input type="text" id="dTags" value="${esc((d.tags || []).join("、"))}">
        <label class="lbl">AI 摘要</label>
        <textarea id="dSummary" rows="4">${esc(d.meeting.summary || "")}</textarea>
        <label class="lbl">逐字稿全文</label>
        <textarea id="dTranscript" rows="12">${esc(d.transcript || "")}</textarea>
        <div class="detail-actions">
          <button class="primary save-detail" data-id="${esc(id)}">儲存</button>
          <button class="ghost cancel-detail">取消</button>
        </div>
      </div>`;
  }
  const decisions = (d.decisions || [])
    .map(x => `<li>${esc(x.description)}${x.context ? `（${esc(x.context)}）` : ""}</li>`).join("");
  const highlights = (d.highlights || [])
    .map(h => `<li class="hl-item detail-hl" data-time="${esc(h.time || "")}" data-quote="${esc(h.source_quote || "")}" title="點擊跳到逐字稿出處">
        <span class="hl-text">${esc(h.text)}</span>
        ${h.time ? `<span class="hl-time">${esc(h.time)}</span>` : ""}
      </li>`).join("");
  const speakers = detectSpeakers(d.transcript, d.meeting.attendees);
  return `<div class="meeting-detail">
      ${d.meeting.summary ? `<h4>AI 摘要 <button class="ghost trans-detail" data-id="${esc(id)}">翻譯</button></h4>
      <p class="detail-summary">${esc(d.meeting.summary)}</p>
      <div class="summary-trans" id="dSummaryTrans" style="display:none"></div>` : ""}
      ${(d.sections || []).length ? `<h4>重點欄位</h4><div class="section-grid">${
        d.sections.map(sec => `<div class="section-card"><h4>${esc(sec.label)}</h4>${
          (sec.items || []).length
            ? `<ul>${sec.items.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`
            : `<p class="empty-note">本次未提及</p>`}</div>`).join("")}</div>` : ""}
      ${highlights ? `<h4>會議重點</h4><ol class="highlight-list">${highlights}</ol>` : ""}
      ${decisions ? `<h4>決議事項</h4><ol class="detail-decisions">${decisions}</ol>` : ""}
      ${speakers.length ? `<h4>講者</h4>
        <div class="speaker-chips">${speakers.map(s =>
          `<span class="speaker-chip" data-id="${esc(id)}" data-speaker="${esc(s)}">✎ ${esc(s)}</span>`).join("")}</div>` : ""}
      <h4>詞彙統一替換</h4>
      <div class="term-tool">
        <div class="term-replace">
          <input type="text" class="term-from" placeholder="原詞">
          <span class="term-arrow">→</span>
          <input type="text" class="term-to" placeholder="改成">
          <button class="ghost term-apply" data-id="${esc(id)}">替換</button>
        </div>
      </div>
      <h4>完整逐字稿 <button class="ghost copy-detail" data-id="${esc(id)}">複製全文</button></h4>
      <div class="detail-transcript" id="dTranscriptView"></div>
      <div class="detail-actions">
        <button class="ghost edit-detail" data-id="${esc(id)}">
          ${icon("square-pen", "i-sm")}
          編輯
        </button>
        <button class="ghost reanalyze-detail" data-id="${esc(id)}">
          ${icon("refresh-cw", "i-sm")}
          重新分析
        </button>
      </div>
    </div>`;
}

const filteredMeetings = () => allMeetings;

// 從首頁或跨會議搜尋跳過來的會議，可能不在目前這一頁。
// 展開之前先把畫面帶到它所在的位置，不然點了會像沒反應。
function focusMeetingPage(id) {
  const at = allMeetings.findIndex(m => m.id === id);
  if (at >= 0) pageNo.meetings = Math.floor(at / PAGE_SIZE.meetings) + 1;
}

function renderMeetings() {
  const p = paginate("meetings", filteredMeetings());
  $("meetingRows").innerHTML = p.items.length
    ? p.items.map(m => `<div class="meeting-item ${m.id === expandedMeetingId ? "expanded" : ""}">
          <span class="meeting-info">
            <b>${esc(m.meeting.title)}</b>
            <span class="meeting-meta mono">${esc(m.meeting.date)}${m.kind ? ` · ${esc(m.kind)}` : ""} · ${esc(m.id)}</span>
            ${(m.tags || []).length ? `<span class="meeting-tags">${m.tags.map(t => `<span class="mtag">${esc(t)}</span>`).join("")}</span>` : ""}
          </span>
          <span class="meeting-ops">
            <button class="ghost view-meeting" data-id="${esc(m.id)}">${m.id === expandedMeetingId ? "收合" : "查閱"}</button>
            <button class="ghost view-full" data-id="${esc(m.id)}" title="用分析結果的版面開啟：代辦卡片、行事曆、確認信、雙欄逐字稿">完整檢視</button>
            <span class="export-wrap">
              <button class="ghost export-toggle" aria-haspopup="menu" aria-expanded="false">分享／匯出</button>
              <span class="export-menu" role="menu" hidden>
                <button class="ws-item share-detail" data-id="${esc(m.id)}" role="menuitem">${icon("share-2", "i-sm")}分享摘要與逐字稿</button>
                <a class="ws-item" href="/api/meetings/${esc(m.id)}/report.md" download role="menuitem">${icon("download", "i-sm")}下載 Markdown</a>
                <a class="ws-item" href="/api/meetings/${esc(m.id)}/events.ics" download role="menuitem">${icon("calendar", "i-sm")}代辦加入行事曆（.ics）</a>
              </span>
            </span>
            <button class="del-btn del-meeting" data-id="${esc(m.id)}" title="刪除此會議與其任務" aria-label="刪除">${icon("x", "i-sm")}</button>
          </span>
        </div>` + (m.id === expandedMeetingId ? meetingDetailHtml(m.id) : "")).join("")
    : `<p class="empty-note">${allMeetings.length ? "沒有符合此標籤的會議" : "尚無會議紀錄"}</p>`;
  renderPager("meetings", p);
  // 逐字稿要等 DOM 建立後渲染
  const view = document.getElementById("dTranscriptView");
  const d = meetingDetailCache[expandedMeetingId];
  if (view && d) {
    if (d.transcript) renderChat(view, d.transcript, d.meeting.attendees);
    else view.innerHTML = `<p class="empty-note">此會議沒有存逐字稿全文</p>`;
  }
  renderHome();
}

async function refreshMeetings() {
  try {
    allMeetings = (await api.listMeetings()).meetings;
    meetingsLoaded = true;
    renderMeetings();
    renderAskScope();
    // 任務庫與主動提醒是依會議標題分組的——它們可能在 allMeetings 載進來之前
    // 就先畫過一輪（群組只顯示 id 備援文字），這裡拿到標題後重畫一次補上。
    // 只重繪、不重新請求。
    if (tasksLoaded) renderTasks();
    if (remindersLoaded) renderReminders();
  } catch (e) {
    meetingsLoaded = true;
    $("meetingRows").innerHTML = loadFail("meetings");
    renderHome();
  }
}

// 完整檢視：用分析結果的視窗開歷史會議。代辦取任務庫現況，確認信與行事曆事件由後端依現況重產
async function openMeetingFull(id) {
  try {
    const [d, { tasks }, notifications] = await Promise.all([
      meetingDetailCache[id] ? Promise.resolve(meetingDetailCache[id]) : api.getMeeting(id),
      api.listTasks(),
      api.meetingNotifications(id),
    ]);
    meetingDetailCache[id] = d;
    renderResult({
      meeting_id: id,
      transcript: d.transcript,
      analysis: {
        meeting: d.meeting,
        decisions: d.decisions || [],
        todos: tasks.filter(t => t.meeting_id === id),
        pending_items: d.pending_items || [],
        highlights: d.highlights || [],
        sections: d.sections || [],
        tags: d.tags || [],
      },
      notifications,
    }, d.transcript, { kind: d.kind, fromHistory: true });
  } catch (err) { showError("開啟會議失敗：" + err.message); }
}

async function openMeetingDetail(id) {
  focusMeetingPage(id);
  expandedMeetingId = id;
  detailEditing = false;
  renderMeetings();  // 先顯示「載入中」
  if (!meetingDetailCache[id]) {
    try {
      meetingDetailCache[id] = await api.getMeeting(id);
    } catch (err) { showError("讀取會議失敗：" + err.message); return; }
  }
  renderMeetings();
}

function meetingShareText(d) {
  const parts = [`${d.meeting.title}（${d.meeting.date}）`];
  if (d.meeting.summary) parts.push(`【AI 摘要】\n${d.meeting.summary}`);
  if ((d.highlights || []).length) {
    parts.push("【會議重點】\n" + d.highlights.map((h, i) =>
      `${i + 1}. ${h.text}${h.time ? `（${h.time}）` : ""}`).join("\n"));
  }
  if ((d.decisions || []).length) {
    parts.push("【決議事項】\n" + d.decisions.map((x, i) => `${i + 1}. ${x.description}`).join("\n"));
  }
  if (d.transcript) parts.push(`【完整逐字稿】\n${d.transcript}`);
  return parts.join("\n\n");
}

function closeExportMenus() {
  document.querySelectorAll(".export-menu").forEach(m => { m.hidden = true; });
  document.querySelectorAll(".export-toggle").forEach(b => b.setAttribute("aria-expanded", "false"));
}
document.addEventListener("click", e => {
  if (!e.target.closest(".export-wrap")) closeExportMenus();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeExportMenus(); });

$("meetingRows").addEventListener("click", async e => {
  const full = e.target.closest(".view-full");
  if (full) { openMeetingFull(full.dataset.id); return; }
  const view = e.target.closest(".view-meeting");
  if (view) {
    if (expandedMeetingId === view.dataset.id) { expandedMeetingId = null; renderMeetings(); }
    else openMeetingDetail(view.dataset.id);
    return;
  }
  const del = e.target.closest(".del-meeting");
  if (del) {
    if (!confirm("確定要刪除這場會議？它的任務也會一併刪除。")) return;
    // 404＝伺服器上早就沒有這場（別的裝置或分頁刪過、雲端重新部署清掉了），是列表過時，
    // 不是刪除失敗：一樣從列表拿掉，講清楚發生什麼事
    let gone = false;
    try {
      await api.deleteMeeting(del.dataset.id);
    } catch (err) {
      if (err.status !== 404) { showError("刪除會議失敗：" + err.message); return; }
      gone = true;
    }
    delete meetingDetailCache[del.dataset.id];
    if (expandedMeetingId === del.dataset.id) expandedMeetingId = null;
    refreshMeetings(); refreshTasks(); refreshReminders();
    // 刪除是丟進垃圾桶，底下那塊要跟著更新——不然使用者會以為東西真的沒了
    REFRESHERS.trash?.(); REFRESHERS.activity?.();
    if (gone) showNotice("這場會議已經不存在（可能已在其他裝置或分頁刪除），已從列表移除。");
    else showNotice("已移到垃圾桶，30 天內可以在歷史會議最下方復原。");
    return;
  }
  // 會議重點 → 跳到逐字稿的時間節點
  const hl = e.target.closest(".detail-hl");
  if (hl) {
    const view = document.getElementById("dTranscriptView");
    if (view) jumpToTranscript(view, hl.dataset.time, hl.dataset.quote);
    return;
  }

  if (e.target.closest(".edit-detail")) { detailEditing = true; renderMeetings(); return; }
  if (e.target.closest(".cancel-detail")) { detailEditing = false; renderMeetings(); return; }

  const save = e.target.closest(".save-detail");
  if (save) {
    try {
      const updated = await api.updateMeeting(save.dataset.id, {
          title: $("dTitle").value.trim() || "未命名會議",
          summary: $("dSummary").value.trim(),
          transcript: $("dTranscript").value,
          tags: $("dTags").value.split(/[、,，\s]+/).map(t => t.trim()).filter(Boolean),
      });
      meetingDetailCache[save.dataset.id] = updated;
      detailEditing = false;
      refreshMeetings();
    } catch (err) { showError("儲存會議失敗：" + err.message); }
    return;
  }

  const rean = e.target.closest(".reanalyze-detail");
  if (rean) {
    const correct = correctTypos();
    const note = correct ? "（含 AI 校正錯字，會改寫逐字稿）" : "";
    if (!confirm(`重新分析會用目前的逐字稿重跑 AI${note}，這場會議的任務會整批換新。繼續？`)) return;
    rean.disabled = true; rean.textContent = "分析中…";
    try {
      const r = await api.reanalyze(rean.dataset.id, { correct_typos: correct });
      const cached = meetingDetailCache[rean.dataset.id];
      if (cached) {
        cached.meeting = r.analysis.meeting;
        cached.decisions = r.analysis.decisions;
        cached.pending_items = r.analysis.pending_items;
        cached.highlights = r.analysis.highlights || [];
        cached.tags = r.analysis.tags || [];
        if (r.corrections && r.corrections.length) cached.transcript = r.transcript;
      }
      if (r.corrections && r.corrections.length) {
        showNotice(`AI 校正了 ${r.corrections.length} 處錯字：` +
          r.corrections.slice(0, 5).map(c => `${c.wrong}→${c.right}`).join("、"));
      }
      refreshMeetings(); refreshTasks(); refreshReminders();
    } catch (err) { showError("重新分析失敗：" + err.message); refreshMeetings(); }
    return;
  }

  const toggle = e.target.closest(".export-toggle");
  if (toggle) {
    const menu = toggle.nextElementSibling;
    const open = menu.hidden;
    closeExportMenus();
    menu.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    return;
  }
  if (e.target.closest(".export-menu")) closeExportMenus();  // 選了任一項就收起來

  const share = e.target.closest(".share-detail");
  if (share) {
    const id = share.dataset.id;
    // 選單在列上，這場會議可能還沒展開過，快取裡沒有內容
    if (!meetingDetailCache[id]) {
      try { meetingDetailCache[id] = await api.getMeeting(id); }
      catch (err) { showError("讀取會議失敗：" + err.message); return; }
    }
    const d = meetingDetailCache[id];
    const text = meetingShareText(d);
    if (navigator.share) {
      try { await navigator.share({ title: d.meeting.title, text }); } catch (err) { /* 使用者取消 */ }
    } else {
      await copyWithFeedback(share, text);  // 桌機沒有系統分享 → 複製全文
    }
    return;
  }

  const copyBtn = e.target.closest(".copy-detail");
  if (copyBtn) {
    const d = meetingDetailCache[copyBtn.dataset.id];
    if (d) await copyWithFeedback(copyBtn, d.transcript || "");
    return;
  }

  const trans = e.target.closest(".trans-detail");
  if (trans) {
    const d = meetingDetailCache[trans.dataset.id];
    const box = document.getElementById("dSummaryTrans");
    if (!d || !d.meeting.summary || !box) return;
    if (box.style.display !== "none") { box.style.display = "none"; return; }
    if (box.textContent) { box.style.display = "block"; return; }
    trans.textContent = "翻譯中…";
    try {
      const r = await api.translate({
          text: d.meeting.summary,
          target: /[一-鿿]/.test(d.meeting.summary) ? "en" : "zh",
        });
      box.textContent = r.translation;
      box.style.display = "block";
    } catch (err) { showError("翻譯失敗：" + err.message); }
    finally { trans.textContent = "翻譯"; }
    return;
  }

  const termBtn = e.target.closest(".term-apply");
  if (termBtn) {
    const id = termBtn.dataset.id;
    const wrap = termBtn.closest(".term-tool");
    const from = wrap.querySelector(".term-from").value.trim();
    const to = wrap.querySelector(".term-to").value;  // 不 trim：允許留空＝把該詞刪掉
    if (!from) { showError("請先輸入要替換的原詞"); return; }
    if (from === to.trim()) { showError("原詞與新詞相同，無需替換"); return; }
    termBtn.disabled = true; termBtn.textContent = "替換中…";
    try {
      const r = await api.replaceTerm(id, { old: from, new: to, add_to_glossary: false });
      if (r.replaced === 0) {
        showNotice(`逐字稿裡找不到「${from}」，沒有任何替換。`);
        termBtn.disabled = false; termBtn.textContent = "替換";
        return;
      }
      meetingDetailCache[id] = r.meeting;
      showNotice(`已把「${from}」替換成「${to || "（刪除）"}」共 ${r.replaced} 處。`);
      renderMeetings();
    } catch (err) {
      showError("詞彙替換失敗：" + err.message);
      termBtn.disabled = false; termBtn.textContent = "替換";
    }
    return;
  }

  const chip = e.target.closest(".speaker-chip");
  if (chip) {
    const id = chip.dataset.id;
    const d = meetingDetailCache[id];
    if (!d || !d.transcript) return;
    const oldName = chip.dataset.speaker;
    const newName = askSpeakerName(oldName);
    if (!newName) return;
    try {
      await renameSpeaker(id, oldName, newName);
      // 摘要換過了，之前翻好的譯文已經過期
      const trans = document.getElementById("dSummaryTrans");
      if (trans) { trans.textContent = ""; trans.style.display = "none"; }
      renderMeetings();
    } catch (err) { showError("講者改名失敗：" + err.message); }
  }
});

refreshTasks();
refreshMeetings();

registerRefresher("meetings", refreshMeetings);
registerPager("meetings", renderMeetings);  // 讓 core 的翻頁按鈕知道要重繪誰

// 切換工作區（個人／群組）：上一個工作區的快取不能留在畫面上，清掉重抓
window.addEventListener("workspacechange", () => {
  for (const id of Object.keys(meetingDetailCache)) delete meetingDetailCache[id];
  allMeetings = [];
  expandedMeetingId = null;
  detailEditing = false;
  pageNo.meetings = 1;
  refreshMeetings();
});

export { allMeetings, askSpeakerName, detailEditing, detectSpeakers, expandedMeetingId, filteredMeetings, focusMeetingPage, meetingDetailCache, meetingDetailHtml, meetingLabel, meetingShareText, meetingsLoaded, openMeetingDetail, openMeetingFull, refreshMeetings, renameSpeaker, renderMeetings };
