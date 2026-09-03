import { api } from "./api.js";
import { renderAskScope } from "./ask.js";
import { $, PAGE_SIZE, esc, icon, loadFail, pageNo, paginate, registerPager, registerRefresher, renderPager, showError, showNotice } from "./core.js";
import { renderHome } from "./home.js";
import { refreshReminders, remindersLoaded, renderReminders } from "./reminders.js";
import { copyWithFeedback } from "./result.js";
import { rememberSpeaker } from "./settings.js";
import { correctTypos, nameSpeakers } from "./setup.js";
import { allTasks, refreshTasks, renderTasks, tasksLoaded } from "./tasks.js";
import { SPEAKER_RE, TIME_RE, jumpToTranscript, renderChat } from "./transcript.js";

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

function detectSpeakers(text) {
  const found = new Set();
  for (const line of String(text || "").split("\n")) {
    const m = line.trim().replace(TIME_RE, "").match(SPEAKER_RE);
    if (m) found.add(m[1].trim());
  }
  return [...found].slice(0, 12);
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
  const speakers = detectSpeakers(d.transcript);
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
      ${speakers.length ? `<h4>講者（點擊改名，整份逐字稿跟著更新）</h4>
        <div class="speaker-chips">${speakers.map(s =>
          `<span class="speaker-chip" data-id="${esc(id)}" data-speaker="${esc(s)}">✎ ${esc(s)}</span>`).join("")}</div>` : ""}
      <h4>詞彙統一替換</h4>
      <div class="term-tool">
        <div class="term-replace">
          <input type="text" class="term-from" placeholder="原詞（聽錯的，如：涵式）">
          <span class="term-arrow">→</span>
          <input type="text" class="term-to" placeholder="改成（如：函式）">
          <button class="ghost term-apply" data-id="${esc(id)}">替換</button>
        </div>
        <div class="term-replace">
          <span class="term-range-lbl">限定時間段（選填）</span>
          <input type="text" class="term-start" placeholder="起 如 12:30">
          <span class="term-arrow">–</span>
          <input type="text" class="term-end" placeholder="迄 如 15:00">
          <span class="glos-note">留空＝整份逐字稿</span>
        </div>
        <label class="chk term-glos"><input type="checkbox" checked> 一併加入詞彙表，之後轉錄不再聽錯</label>
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
        <button class="ghost share-detail" data-id="${esc(id)}">
          ${icon("share-2", "i-sm")}
          分享
        </button>
        <a class="ghost btn-link" href="/api/meetings/${esc(id)}/events.ics" download>加入行事曆</a>
      </div>
    </div>`;
}

let activeTag = "";  // 歷史會議標籤篩選（"" = 全部）
let tagsExpanded = false;  // AI 每場會議都自己取標籤，久了會很雜——預設只顯示最常用的幾個
const TAG_FILTER_LIMIT = 12;

function renderTagFilter() {
  const counts = new Map();
  allMeetings.forEach(m => {
    if (m.kind) counts.set(m.kind, (counts.get(m.kind) || 0) + 1);
    (m.tags || []).forEach(t => counts.set(t, (counts.get(t) || 0) + 1));
  });
  const bar = $("tagFilter");
  if (!counts.size) { bar.style.display = "none"; return; }
  if (activeTag && !counts.has(activeTag)) activeTag = "";
  bar.style.display = "flex";

  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([t]) => t);
  const overflow = sorted.length - TAG_FILTER_LIMIT;
  let visible = overflow <= 0 || tagsExpanded ? sorted : sorted.slice(0, TAG_FILTER_LIMIT);
  // 目前選中的標籤若被截掉了也要顯示，不然使用者會看不到自己選的是哪個
  if (activeTag && !visible.includes(activeTag)) visible = [...visible, activeTag];

  bar.innerHTML = [`<span class="tag-chip ${activeTag ? "" : "active"}" data-tag="">全部</span>`]
    .concat(visible.map(t =>
      `<span class="tag-chip ${t === activeTag ? "active" : ""}" data-tag="${esc(t)}">${esc(t)}</span>`))
    .concat(overflow > 0
      ? [`<span class="tag-chip tag-more" data-more="1">${tagsExpanded ? "收合" : `更多 +${overflow}`}</span>`]
      : [])
    .join("");
}

const filteredMeetings = () => allMeetings.filter(m =>
  !activeTag || m.kind === activeTag || (m.tags || []).includes(activeTag));

// 從首頁或跨會議搜尋跳過來的會議，可能不在目前這一頁，甚至被標籤篩掉了。
// 展開之前先把畫面帶到它所在的位置，不然點了會像沒反應。
function focusMeetingPage(id) {
  if (!allMeetings.some(m => m.id === id)) return;
  if (!filteredMeetings().some(m => m.id === id)) activeTag = "";
  const at = filteredMeetings().findIndex(m => m.id === id);
  if (at >= 0) pageNo.meetings = Math.floor(at / PAGE_SIZE.meetings) + 1;
}

function renderMeetings() {
  renderTagFilter();
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
            <a class="ghost btn-link" href="/api/meetings/${esc(m.id)}/report.md" download>下載</a>
            <button class="del-btn del-meeting" data-id="${esc(m.id)}" title="刪除此會議與其任務" aria-label="刪除">${icon("x", "i-sm")}</button>
          </span>
        </div>` + (m.id === expandedMeetingId ? meetingDetailHtml(m.id) : "")).join("")
    : `<p class="empty-note">${allMeetings.length ? "沒有符合此標籤的會議" : "尚無會議紀錄"}</p>`;
  renderPager("meetings", p);
  // 逐字稿要等 DOM 建立後渲染
  const view = document.getElementById("dTranscriptView");
  const d = meetingDetailCache[expandedMeetingId];
  if (view && d) {
    if (d.transcript) renderChat(view, d.transcript);
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

$("meetingRows").addEventListener("click", async e => {
  const view = e.target.closest(".view-meeting");
  if (view) {
    if (expandedMeetingId === view.dataset.id) { expandedMeetingId = null; renderMeetings(); }
    else openMeetingDetail(view.dataset.id);
    return;
  }
  const del = e.target.closest(".del-meeting");
  if (del) {
    if (!confirm("確定要刪除這場會議？它的任務也會一併刪除。")) return;
    try {
      await api.deleteMeeting(del.dataset.id);
      delete meetingDetailCache[del.dataset.id];
      if (expandedMeetingId === del.dataset.id) expandedMeetingId = null;
      refreshMeetings(); refreshTasks(); refreshReminders();
    } catch (err) { showError("刪除會議失敗：" + err.message); }
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
      const r = await api.reanalyze(rean.dataset.id, { correct_typos: correct, name_speakers: nameSpeakers(),
      });
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

  const share = e.target.closest(".share-detail");
  if (share) {
    const d = meetingDetailCache[share.dataset.id];
    if (!d) return;
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
    const start = wrap.querySelector(".term-start").value.trim();
    const end = wrap.querySelector(".term-end").value.trim();
    const addGlos = wrap.querySelector(".term-glos input").checked;
    if (!from) { showError("請先輸入要替換的原詞"); return; }
    if (from === to.trim()) { showError("原詞與新詞相同，無需替換"); return; }
    const rangeNote = (start || end) ? `（時間段 ${start || "開頭"}–${end || "結尾"}）` : "";
    termBtn.disabled = true; termBtn.textContent = "替換中…";
    try {
      const r = await api.replaceTerm(id, { old: from, new: to, add_to_glossary: addGlos, start: start || null, end: end || null });
      if (r.replaced === 0) {
        showNotice(`${rangeNote ? "指定時間段裡" : "逐字稿裡"}找不到「${from}」，沒有任何替換。`);
        termBtn.disabled = false; termBtn.textContent = "替換";
        return;
      }
      meetingDetailCache[id] = r.meeting;
      showNotice(`已把「${from}」替換成「${to || "（刪除）"}」共 ${r.replaced} 處${rangeNote}` +
        (r.glossary_added ? "，並已加入詞彙表" : "") + "。");
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
    const newName = (prompt(`把「${oldName}」改名為：`, oldName) || "").trim();
    if (!newName || newName === oldName) return;
    const renamed = d.transcript.split("\n").map(line => {
      const t = line.trimStart().replace(TIME_RE, "");  // 行首可能還有 [1:02] 時間標記
      if (t.startsWith(oldName + "：") || t.startsWith(oldName + ":")) {
        return line.replace(oldName, newName);  // 只換行首的講者標註
      }
      return line;
    }).join("\n");
    // 出席者名單裡的舊名一併換成新名（下游不再殘留「講者A」）
    const attendees = (d.meeting.attendees || []).map(a => (a === oldName ? newName : a));
    try {
      meetingDetailCache[id] = await api.updateMeeting(id, { transcript: renamed, attendees });
      // 這場會議中「負責人＝舊名」的任務也跟著改名
      const owned = allTasks.filter(t => t.meeting_id === id && t.owner === oldName);
      for (const t of owned) {
        await api.updateTask(t.id, { owner: newName });
      }
      rememberSpeaker(newName);  // 記進名冊，下次辨識講者時姓名寫法就有依據
      renderMeetings();
      if (owned.length) { refreshTasks(); refreshReminders(); }
    } catch (err) { showError("講者改名失敗：" + err.message); }
  }
});

refreshTasks();
refreshMeetings();

// 標籤篩選
$("tagFilter").addEventListener("click", e => {
  if (e.target.closest("[data-more]")) { tagsExpanded = !tagsExpanded; renderTagFilter(); return; }
  const chip = e.target.closest(".tag-chip");
  if (!chip) return;
  activeTag = chip.dataset.tag;
  pageNo.meetings = 1;  // 換標籤等於換一份清單，從第一頁看起
  renderMeetings();
});

registerRefresher("meetings", refreshMeetings);
registerPager("meetings", renderMeetings);  // 讓 core 的翻頁按鈕知道要重繪誰

export { TAG_FILTER_LIMIT, activeTag, allMeetings, detailEditing, detectSpeakers, expandedMeetingId, filteredMeetings, focusMeetingPage, meetingDetailCache, meetingDetailHtml, meetingLabel, meetingShareText, meetingsLoaded, openMeetingDetail, refreshMeetings, renderMeetings, renderTagFilter, tagsExpanded };
