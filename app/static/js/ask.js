import { api } from "./api.js";
import { $, esc, icon } from "./core.js";
import { allMeetings, openMeetingDetail } from "./meetings.js";
import { showView } from "./router.js";
import { jumpToTranscript } from "./transcript.js";

/* ==================================================================
   8. 跨會議問答：RAG 問答＋關鍵字即時搜尋
   ================================================================== */
// ---- 回答附帶的東西：AI 理解的條件、相關會議與段落（點了跳到逐字稿那一行） ----
function conditionsHtml(c) {
  if (!c) return "";
  const chips = [];
  if (c.date_from || c.date_to) {
    chips.push(`${icon("calendar", "i-sm")}${esc(c.date_from || "…")} ~ ${esc(c.date_to || "…")}`);
  }
  (c.kinds || []).forEach(k => chips.push(esc(k)));
  (c.people || []).forEach(p => chips.push(`${icon("user", "i-sm")}${esc(p)}`));
  return `<div class="ask-cond" title="AI 從問題裡解析出的條件，只在符合的會議裡找">${
    chips.map(x => `<span class="meta-chip">${x}</span>`).join("")}</div>`;
}

// 後端挑好的焦點行（最能回答問題的那一行）。跳轉比對的是泡泡內文，講者是分開顯示的，要去掉
function passageQuote(p) {
  const q = p.quote || "";
  const colon = q.search(/[：:]/);
  return (colon >= 0 && colon <= 20 ? q.slice(colon + 1) : q).trim().slice(0, 40);
}

// 逐字稿段落顯示焦點行；摘要卡沒有行可挑，顯示開頭
const passageSnippet = p => (p.quote || String(p.text || "").replace(/\s+/g, " ")).slice(0, 140);

function passagesHtml(passages) {
  if (!passages || !passages.length) return "";
  const groups = new Map();
  passages.forEach(p => {
    if (!groups.has(p.meeting_id)) groups.set(p.meeting_id, { title: p.title, date: p.date, items: [] });
    groups.get(p.meeting_id).items.push(p);
  });
  return `<div class="ask-passages"><span class="ask-hits-head">相關會議與段落（點擊跳到逐字稿）</span>${
    [...groups.entries()].map(([id, g]) => `<div class="ask-group">
        <div class="ask-group-head"><b>${esc(g.title)}</b><span class="meta">${esc(g.date)}</span></div>
        ${g.items.map(p => `<div class="ask-passage${p.cited ? " cited" : ""}" data-id="${esc(id)}"
            data-time="${esc(p.time || "")}" data-quote="${esc(passageQuote(p))}">
            ${p.time ? `<span class="p-time">${esc(p.time)}</span>` : ""}
            ${p.source === "summary" ? `<span class="p-tag">摘要</span>` : ""}
            <span class="snippet" title="${esc(p.text)}">${esc(passageSnippet(p))}</span>
            ${p.cited ? `<span class="p-tag cited">引用</span>` : ""}
          </div>`).join("")}
      </div>`).join("")}</div>`;
}


// ---- 跨會議問答（RAG） ----
// 範圍複選：勾了哪些會議就只在那些會議裡檢索；都不勾 = 全部
const askScopeIds = new Set();

function renderAskScope() {
  const list = $("askScopeList");
  [...askScopeIds].forEach(id => {  // 會議被刪掉時同步移除
    if (!allMeetings.some(m => m.id === id)) askScopeIds.delete(id);
  });
  list.innerHTML = allMeetings.length
    ? allMeetings.map(m => `<label>
        <input type="checkbox" value="${esc(m.id)}" ${askScopeIds.has(m.id) ? "checked" : ""}>
        <span>${esc(m.meeting.title)}</span>
        <span class="meta">${esc(m.meeting.date)}</span>
      </label>`).join("")
    : `<p class="empty-note">尚無會議</p>`;
  $("askScopeSummary").textContent = askScopeIds.size
    ? `選取會議（已選 ${askScopeIds.size} 場）`
    : "選取會議";
}

$("askScopeList").addEventListener("change", e => {
  const cb = e.target.closest("input[type=checkbox]");
  if (!cb) return;
  if (cb.checked) askScopeIds.add(cb.value);
  else askScopeIds.delete(cb.value);
  renderAskScope();
});

async function sendAsk() {
  const q = $("askInput").value.trim();
  if (!q) return;
  $("askInput").value = "";
  hideSearchHits();
  const log = $("askLog");
  log.style.display = "flex";
  log.insertAdjacentHTML("beforeend",
    `<div class="ask-item">
       <button class="del-btn del-ask" title="刪除這則問答" aria-label="刪除">${icon("x", "i-sm")}</button>
       <div class="ask-q"><span>Q</span><div>${esc(q)}</div></div>
       <div class="ask-a pending"><span>A</span><div>檢索會議紀錄中…</div></div>
     </div>`);
  const slot = log.lastElementChild.querySelector(".ask-a");
  log.scrollTop = log.scrollHeight;
  $("btnAsk").disabled = true;
  try {
    const r = await api.ask({
        question: q,
        meeting_ids: askScopeIds.size ? [...askScopeIds] : null,
      });
    slot.classList.remove("pending");
    slot.innerHTML = `<span>A</span><div>${conditionsHtml(r.conditions)}${esc(r.answer)}${passagesHtml(r.passages)}</div>`;
  } catch (err) {
    slot.classList.remove("pending");
    slot.classList.add("err");
    slot.innerHTML = `<span>!</span><div>${esc(err.message)}</div>`;
  } finally {
    $("btnAsk").disabled = false;
    log.scrollTop = log.scrollHeight;
  }
}
$("btnAsk").addEventListener("click", sendAsk);
$("askInput").addEventListener("keydown", e => { if (e.key === "Enter") sendAsk(); });

// ---- 一框兩用：打字即時關鍵字搜尋（精確比對），按查詢才是問 AI ----
let searchTimer = null, searchSeq = 0;

function hideSearchHits() { $("askSearchHits").style.display = "none"; }

function snippetHtml(snippet, keyword) {
  const idx = snippet.toLowerCase().indexOf(keyword.toLowerCase());
  if (idx < 0) return esc(snippet);
  return esc(snippet.slice(0, idx)) +
    `<mark>${esc(snippet.slice(idx, idx + keyword.length))}</mark>` +
    esc(snippet.slice(idx + keyword.length));
}

$("askInput").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const kw = $("askInput").value.trim();
  if (kw.length < 2) { hideSearchHits(); return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    try {
      const r = await api.search(kw);
      if (seq !== searchSeq) return;  // 已有更新的搜尋，丟棄舊結果
      const box = $("askSearchHits");
      if (!r.hits.length) { hideSearchHits(); return; }
      box.innerHTML =
        `<span class="ask-hits-head">含「${esc(r.keyword)}」的會議（點擊開啟）</span>` +
        r.hits.map(h => `<div class="ask-hit" data-id="${esc(h.meeting_id)}">
            <b>${esc(h.title)}</b>
            <span class="snippet">${snippetHtml(h.snippet, r.keyword)}</span>
            <span class="meta">${esc(h.date)} · ${esc(h.field)}</span>
          </div>`).join("");
      box.style.display = "flex";
    } catch (err) { hideSearchHits(); }
  }, 300);
});

$("askSearchHits").addEventListener("click", e => {
  const hit = e.target.closest(".ask-hit");
  if (!hit) return;
  hideSearchHits();
  showView("meeting");
  openMeetingDetail(hit.dataset.id);
});
// 每則問答各自刪除；刪到全空就把整個對話框收起來
$("askLog").addEventListener("click", async e => {
  const passage = e.target.closest(".ask-passage");
  if (passage) {
    const { id, time, quote } = passage.dataset;
    showView("meeting");
    await openMeetingDetail(id);
    const view = document.getElementById("dTranscriptView");
    if (view && (time || quote)) {
      view.scrollIntoView({ behavior: "smooth", block: "start" });
      jumpToTranscript(view, time, quote);
    }
    return;
  }
  const del = e.target.closest(".del-ask");
  if (!del) return;
  del.closest(".ask-item").remove();
  const log = $("askLog");
  if (!log.querySelector(".ask-item")) log.style.display = "none";
});

export { askScopeIds, conditionsHtml, hideSearchHits, passageQuote, passagesHtml, renderAskScope, searchTimer, sendAsk, snippetHtml };
