import { $, PRIORITY_ZH, esc, icon, jsonOrThrow, showError, showNotice, skelBlocks, skelLine } from "./core.js";
import { refreshMeetings } from "./meetings.js";
import { refreshReminders } from "./reminders.js";
import { WIDE, showView } from "./router.js";
import { maybePromoteTerms } from "./setup.js";
import { refreshTasks } from "./tasks.js";
import { jumpToTranscript, renderChat } from "./transcript.js";

/* ==================================================================
   4. 分析結果：摘要、會議重點、決議、代辦、行事曆、確認信
   ================================================================== */
// ---- AI 校正的錯字清單：讓使用者看得到到底改了哪些字，不是黑箱 ----
function renderCorrections(corrections) {
  const sec = $("rCorrSec");
  sec.style.display = corrections.length ? "block" : "none";
  sec.open = false;
  $("corrCount").textContent = corrections.length || "";
  $("rCorrections").innerHTML = corrections.map(c => `
    <div class="corr-item">
      <span class="corr-from">${esc(c.wrong)}</span>
      <span class="corr-arrow">→</span>
      <span class="corr-to">${esc(c.right)}</span>
      ${c.count > 1 ? `<span class="corr-count">×${c.count}</span>` : ""}
      ${c.reason ? `<span class="corr-why">${esc(c.reason)}</span>` : ""}
    </div>`).join("");
}

// ---- 結果渲染 ----
let currentTranscript = "";
let analysisStartTime = null;
// 三條輸入路徑都要在送出當下記時間；imported binding 不能賦值，所以給一個 setter
function markAnalysisStart() { analysisStartTime = Date.now(); }

// AI 分析要跑十幾秒到一分鐘。與其讓人盯著一顆 disabled 的按鈕，不如先把報告的
// 版面輪廓擺出來：既回饋「系統在做事」，也預告等一下會拿到哪些區塊。
// renderResult 會把每一格覆蓋掉，所以這裡填什麼都不會殘留。
function showResultSkeleton() {
  if ($("result").classList.contains("is-loading")) return;  // 輪詢會重複呼叫，別一直把畫面捲回頂端
  const lines = n => Array.from({ length: n }, (_, i) => skelLine(`${94 - i * 12}%`)).join("");
  $("rTitle").innerHTML = skelLine("46%");
  $("rMeta").innerHTML = `${skelLine("92px")}${skelLine("64px")}${skelLine("120px")}`;
  $("rSummary").innerHTML = lines(3);
  $("rHighlights").innerHTML = skelBlocks(3);
  $("rDecisions").innerHTML = skelBlocks(2);
  $("rTodos").innerHTML = skelBlocks(3);
  $("rPending").innerHTML = skelBlocks(1);
  $("rEvents").innerHTML = skelBlocks(1);
  $("rDraft").innerHTML = lines(4);
  ["hSummary", "hHighlights", "hDecisions", "hTodos"].forEach(id => ($(id).style.display = "flex"));
  $("rSummary").style.display = "block";
  $("rHighlights").style.display = "flex";
  $("rDecisions").style.display = "flex";
  $("rTodos").style.display = "flex";
  $("rSummaryTrans").style.display = "none";
  $("hSections").style.display = "none";
  $("rSections").style.display = "none";
  $("rTransSec").style.display = "none";
  $("rCorrSec").style.display = "none";
  $("result").classList.add("is-loading");
  $("result").style.display = "flex";
  $("navResult").hidden = false;
  showView("result");
}

// 分析失敗：骨架整個收掉退回「新會議」。上一份結果這時也已經過期了，一併清掉，
// 免得使用者以為那是這次跑出來的
function hideResultSkeleton() {
  if (!$("result").classList.contains("is-loading")) return;
  $("result").classList.remove("is-loading");
  $("result").style.display = "none";
  $("navResult").hidden = true;
  showView("new");
}

// 種類專屬區塊：後端已經把 label 與順序排好了，前端只負責渲染，
// 不認得任何特定欄位名稱——之後新增種類不用再改這裡
function renderSections(sections) {
  const has = (sections || []).length > 0;
  $("hSections").style.display = has ? "flex" : "none";
  $("rSections").style.display = has ? "grid" : "none";
  if (!has) { $("rSections").innerHTML = ""; return; }
  $("rSections").innerHTML = sections.map(sec => `
    <div class="section-card">
      <h4>${esc(sec.label)}</h4>
      ${(sec.items || []).length
        ? `<ul>${sec.items.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`
        : `<p class="empty-note">本次未提及</p>`}
    </div>`).join("");
}

function renderResult(result, transcript) {
  $("result").classList.remove("is-loading");
  maybePromoteTerms();
  // 後端校正過的話，result.transcript 才是最終版本（傳進來的可能是校正前的）
  currentTranscript = (result.transcript || transcript || "").trim();
  $("rTransSec").style.display = currentTranscript ? "block" : "none";
  // 雙欄版面下逐字稿是常駐對照欄，預設攤開；單欄（窄螢幕）才收起來免得洗版
  $("rTransSec").open = WIDE.matches;
  renderChat($("rTranscript"), currentTranscript);
  renderCorrections(result.corrections || []);
  const a = result.analysis, m = a.meeting;
  // 成效指標：這場會議 AI 幫你做了多少事、花了多久
  const elapsed = analysisStartTime ? ((Date.now() - analysisStartTime) / 1000).toFixed(1) : null;
  analysisStartTime = null;
  const statsChip =
    `<span class="meta-chip stats-chip">${currentTranscript ? `${currentTranscript.length} 字 → ` : ""}` +
    `${a.todos.length} 任務・${a.decisions.length} 決議${elapsed ? `・${elapsed}s` : ""}</span>`;
  $("rTitle").textContent = m.title;
  $("rMeta").innerHTML =
    `<span class="meta-chip">${icon("calendar", "i-sm")}${esc(m.date)}</span>` +
    `<span class="meta-chip">${esc($("meetingKind").value)}</span>` +
    m.attendees.map(p => `<span class="meta-chip">${icon("user", "i-sm")}${esc(p)}</span>`).join("") +
    (a.tags || []).map(t => `<span class="meta-chip"><span class="mtag">${esc(t)}</span></span>`).join("") +
    statsChip;
  // 摘要／重點／決議／代辦：這場會議的種類沒產出這個區塊時整節隱藏。
  // 改版後每種類都有自己的預設區塊，所以一律改成「有內容才顯示」，
  // 不再把某一個種類寫死在前端
  $("hSummary").style.display = m.summary ? "flex" : "none";
  $("rSummary").style.display = m.summary ? "block" : "none";
  $("rSummary").textContent = m.summary || "";
  // 摘要翻譯：中文摘要→譯成英文，外文摘要→譯成中文
  $("rSummaryTrans").style.display = "none";
  $("rSummaryTrans").textContent = "";
  $("transSummaryLabel").textContent = /[一-鿿]/.test(m.summary || "") ? "譯成英文" : "譯成中文";

  $("sectionsTitle").textContent = `${$("meetingKind").value}重點`;
  renderSections(a.sections);

  const highlights = a.highlights || [];
  const showHighlights = highlights.length > 0;
  $("hHighlights").style.display = showHighlights ? "flex" : "none";
  $("rHighlights").style.display = showHighlights ? "flex" : "none";
  $("rHighlights").innerHTML = highlights.length
    ? highlights.map(h => `
      <li class="hl-item" data-time="${esc(h.time || "")}" data-quote="${esc(h.source_quote || "")}" title="點擊跳到逐字稿出處">
        <span class="hl-text">${esc(h.text)}</span>
        ${h.time ? `<span class="hl-time">${esc(h.time)}</span>` : ""}
      </li>`).join("")
    : `<p class="empty-note">未擷取到會議重點</p>`;

  const showDecisions = a.decisions.length > 0;
  $("hDecisions").style.display = showDecisions ? "flex" : "none";
  $("rDecisions").style.display = showDecisions ? "flex" : "none";
  $("rDecisions").innerHTML = a.decisions.length
    ? a.decisions.map(d => `<li>${esc(d.description)}${d.context ? ` <span class="ctx">（${esc(d.context)}）</span>` : ""}</li>`).join("")
    : `<p class="empty-note">本次會議無正式決議</p>`;

  const showTodos = a.todos.length > 0;
  $("hTodos").style.display = showTodos ? "flex" : "none";
  $("rTodos").style.display = showTodos ? "flex" : "none";
  $("rTodos").innerHTML = a.todos.length
    ? a.todos.map(t => `
      <div class="todo-card p-${t.priority}">
        <div class="todo-head"><span class="led"></span><span class="todo-task">${esc(t.task)}</span></div>
        <div class="badges">
          <span class="badge ${t.owner ? "" : "owner-none"}">負責人 <b>${esc(t.owner || "未指派")}</b></span>
          <span class="badge">期限 <b>${esc(t.due_date || "未定")}</b></span>
          <span class="badge pr-${t.priority}" ${t.priority_reason ? `title="${esc(t.priority_reason)}"` : ""}>優先級 <b>${PRIORITY_ZH[t.priority]}</b></span>
        </div>
        ${t.priority_reason ? `<p class="why">判斷依據：${esc(t.priority_reason)}</p>` : ""}
        ${t.source_quote ? `<p class="quote clickable" data-quote="${esc(t.source_quote)}" title="點擊跳到逐字稿出處">${esc(t.source_quote)}</p>` : ""}
      </div>`).join("")
    : `<p class="empty-note">未偵測到代辦事項</p>`;

  $("rPending").innerHTML = a.pending_items.length
    ? a.pending_items.map(p => `<div class="pending-item">${icon("circle-help")}<span>${esc(p.topic)}${p.reason ? `<span class="reason">${esc(p.reason)}</span>` : ""}</span></div>`).join("")
    : `<p class="empty-note">無</p>`;

  $("icsLink").href = `/api/meetings/${encodeURIComponent(result.meeting_id)}/events.ics`;
  const events = result.notifications.calendar_events || [];
  $("rEvents").innerHTML = events.length
    ? events.map(e => `<div class="event-item">${icon("calendar")}<span><span class="when">${esc(e.start.date)}</span><b>${esc(e.summary)}</b><span class="desc">${esc(e.description).replace(/\n/g, " · ")}</span></span></div>`).join("")
    : `<p class="empty-note">沒有含期限的代辦，未產生行事曆事件</p>`;

  $("rDraft").textContent = result.notifications.email_draft || "";
  draftSubject = result.notifications.email_subject || "";
  $("result").style.display = "flex";
  $("navResult").hidden = false;
  showView("result");
  refreshTasks();
  refreshMeetings();
  refreshReminders();
}

// 複製按鈕通用行為：複製後短暫顯示「已複製」
async function copyWithFeedback(btn, text) {
  await navigator.clipboard.writeText(text);
  const original = btn.innerHTML;
  btn.textContent = "已複製";
  setTimeout(() => (btn.innerHTML = original), 1500);
}
$("btnCopyDraft").addEventListener("click", () => copyWithFeedback($("btnCopyDraft"), $("rDraft").textContent));

// ---- 確認信一鍵開信 ----
// 收件人刻意留空（出席者名單在信件內文裡，姓名對不到 email），使用者在信件視窗自己選。
// 主旨與內文則用網址參數帶進去。中文經 encodeURIComponent 後一個字會膨脹成 9 個字元，
// 一份含十幾項代辦的草稿很容易把網址撐到上萬字元，所以兩條路徑各有長度上限：
// mailto: 交給作業系統的郵件軟體處理，上限最低（Windows 實測約 2000 字元就會被截斷）；
// Gmail 網頁版寬鬆得多。超過就改走「複製全文到剪貼簿 + 只帶主旨開信」——
// 讓使用者按一下貼上，好過寄出一封內容被砍一半的信。
const MAILTO_URL_LIMIT = 1800;
const GMAIL_URL_LIMIT = 7000;

let draftSubject = "";  // 由 renderResult 從 notifications.email_subject 帶入

function draftSubjectAndBody() {
  const draft = $("rDraft").textContent || "";
  const lines = draft.split("\n");
  // 主旨會另外填進信件的主旨欄，內文再放一次會重複；順手吃掉它後面的空行
  let subject = draftSubject;
  if (lines[0] && lines[0].startsWith("主旨：")) {
    if (!subject) subject = lines[0].slice(3);  // 舊會議的草稿沒有 email_subject 欄位
    lines.shift();
    while (lines.length && !lines[0].trim()) lines.shift();
  }
  return { subject: subject || "會議紀錄確認", body: lines.join("\n"), draft };
}

async function openCompose(via) {
  const { subject, body, draft } = draftSubjectAndBody();
  if (!draft.trim()) return;
  const build = b => via === "gmail"
    ? `https://mail.google.com/mail/?view=cm&fs=1&su=${encodeURIComponent(subject)}&body=${encodeURIComponent(b)}`
    : `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(b)}`;

  let url = build(body);
  if (url.length > (via === "gmail" ? GMAIL_URL_LIMIT : MAILTO_URL_LIMIT)) {
    url = build("");
    try {
      await navigator.clipboard.writeText(draft);
      showNotice("信件內容太長，無法帶進網址：已複製全文到剪貼簿，請在信件裡直接貼上。");
    } catch {
      showError("信件內容太長，無法帶進網址：請改按「複製」再自行貼到信件裡。");
      return;
    }
  }
  // mailto 交給系統處理（不會真的離開頁面）；Gmail 是網頁，開新分頁才不會蓋掉分析結果
  if (via === "gmail") window.open(url, "_blank", "noopener");
  else window.location.href = url;
}

$("btnGmailDraft").addEventListener("click", () => openCompose("gmail"));
$("btnMailtoDraft").addEventListener("click", () => openCompose("mailto"));

// 摘要翻譯（再按一次收起）
$("btnTransSummary").addEventListener("click", async () => {
  const box = $("rSummaryTrans");
  if (box.style.display !== "none") { box.style.display = "none"; return; }
  if (box.textContent) { box.style.display = "block"; return; }  // 已翻過，直接展開
  const summary = $("rSummary").textContent.trim();
  if (!summary) return;
  const target = /[一-鿿]/.test(summary) ? "en" : "zh";
  const label = $("transSummaryLabel");
  const original = label.textContent;
  label.textContent = "翻譯中…";
  try {
    const r = await jsonOrThrow(await fetch("/api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: summary, target }),
    }));
    box.textContent = r.translation;
    box.style.display = "block";
  } catch (err) { showError("翻譯失敗：" + err.message); }
  finally { label.textContent = original; }
});
$("btnCopyTranscript").addEventListener("click", e => {
  e.preventDefault();  // 按鈕在 <summary> 裡，避免同時觸發展開/收合
  e.stopPropagation();
  copyWithFeedback($("btnCopyTranscript"), currentTranscript);
});

// ---- 逐字稿對照：點任務卡的引用句 → 展開逐字稿、標亮出處行 ----
$("rTodos").addEventListener("click", e => {
  const q = e.target.closest(".quote.clickable");
  if (!q || !currentTranscript) return;
  const sec = $("rTransSec");
  sec.style.display = "block";
  sec.open = true;
  if (!jumpToTranscript($("rTranscript"), null, q.dataset.quote)) {
    sec.scrollIntoView({ behavior: "smooth", block: "nearest" });  // 找不到就只展開逐字稿
  }
});

// ---- 會議重點：點擊 → 展開逐字稿、跳到該時間節點（沒有時間就用原句比對） ----
$("rHighlights").addEventListener("click", e => {
  const item = e.target.closest(".hl-item");
  if (!item || !currentTranscript) return;
  const sec = $("rTransSec");
  sec.style.display = "block";
  sec.open = true;
  if (!jumpToTranscript($("rTranscript"), item.dataset.time, item.dataset.quote)) {
    sec.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
});

export { GMAIL_URL_LIMIT, MAILTO_URL_LIMIT, analysisStartTime, copyWithFeedback, currentTranscript, draftSubject, draftSubjectAndBody, hideResultSkeleton, markAnalysisStart, openCompose, renderCorrections, renderResult, renderSections, showResultSkeleton };
