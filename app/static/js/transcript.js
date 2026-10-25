import { $, esc } from "./core.js";
import { LABEL_RE } from "./speakers.js";
import { parseChatMessages } from "./transcriptedit.js";

/* ==================================================================
   3. 逐字稿：連續文件式渲染（時間欄＋講者＋內文）與時間/引用句跳轉
   ================================================================== */
// ---- 連續文件式逐字稿渲染 ----
// 偵測行首「[1:02]」時間標記與「講者A：」「王小明：」等講者前綴（哪些算講者見 speakers.js）。
// 帶時間標記的行自成一句（時間軸不能被合併吃掉）；沒有時間標記時維持
// 「同講者連續行合併」——轉錄結果常常一句一行，不合併會太碎。
// 行首時間標記。各段放寬成 1~2 位數：模型會吐出 [00] 與 [0:1]（後端 segments.py 同一套規則）
const TIME_RE = /^\[(\d{1,2}(?::\d{1,2}){0,2})\]\s*/;

// "1:02" / "1:02:03" → 秒數
function timeLabelToSeconds(label) {
  return String(label).split(":").reduce((acc, p) => acc * 60 + Number(p), 0);
}

// 泡泡怎麼切（含講者標籤的繼承規則）只有 transcriptedit.js 一份：就地編輯要靠
// 「畫面上第幾個泡泡 ＝ 原文哪幾行」對得起來，兩邊各切一次遲早會不一致

// speakerMap：跨段落維持同講者同色（即時聆聽逐段附加時傳入同一個 map）
// translation：對應的譯文（可選）。行數對得上就逐句雙語對照，對不上就整段附在最後。
function chatHtml(text, speakerMap, translation, knownNames = []) {
  const map = speakerMap || {};
  const colorOf = s => {
    if (!(s in map)) map[s] = Object.keys(map).length;
    return map[s] % 5;
  };
  const msgs = parseChatMessages(text, knownNames);
  // 譯文沒有時間標記、合併方式可能與原文不同：先切成非空行，行數與原文
  // 總行數一致時，照原文每句合併的行數分組對齊
  let transTexts = null;
  if (translation) {
    const tLines = String(translation).split("\n")
      .map(l => l.trim().replace(TIME_RE, "")).filter(Boolean)
      .map(l => { const m = l.match(LABEL_RE); return m ? l.slice(m[0].length) : l; });
    const total = msgs.reduce((n, m) => n + m.lines, 0);
    if (msgs.length && tLines.length === total) {
      transTexts = []; let i = 0;
      for (const m of msgs) { transTexts.push(tLines.slice(i, i + m.lines).join(" ")); i += m.lines; }
    }
  }
  let html = "", prevSpeaker = null;
  msgs.forEach((m, i) => {
    const showName = m.speaker && m.speaker !== prevSpeaker;
    prevSpeaker = m.speaker;
    html += `<div class="chat-line ${m.speaker ? `sp-${colorOf(m.speaker)}` : ""}" data-i="${i}"${
        m.time ? ` data-t="${timeLabelToSeconds(m.time)}"` : ""}>` +
      `<span class="line-time">${m.time ? esc(m.time) : ""}</span>` +
      `<span class="line-body">${showName ? `<b class="line-speaker">${esc(m.speaker)}</b>` : ""}${esc(m.text)}` +
      (transTexts ? `<div class="line-trans">${esc(transTexts[i])}</div>` : "") +
      `</span></div>`;
  });
  if (translation && !transTexts) {  // 行數對不上：譯文整段補在後面
    html += `<div class="chat-line"><span class="line-time"></span><span class="line-body"><div class="line-trans">${esc(translation)}</div></span></div>`;
  }
  return html;
}

function renderChat(container, text, knownNames = []) {
  container.innerHTML = chatHtml(text, {}, null, knownNames);
}

// ---- 逐字稿跳轉：依時間節點（優先）或引用句找到對應行，標亮並捲到可視範圍 ----
function findLineByQuote(lines, rawQuote) {
  // 引用句可能被截斷、改寫或跨行，逐步縮短前綴比對
  let quote = (rawQuote || "").replace(/\s+/g, "");
  let target = null;
  while (!target && quote.length > 4) {
    target = lines.find(l => l.textContent.replace(/\s+/g, "").includes(quote));
    if (!target) quote = quote.slice(0, Math.floor(quote.length * 0.7));
  }
  return target;
}

function jumpToTranscript(container, timeLabel, quote) {
  const lines = [...container.querySelectorAll(".chat-line")];
  lines.forEach(l => l.classList.remove("hl"));
  let target = null;
  if (timeLabel) {
    const t = timeLabelToSeconds(timeLabel);
    // 逐字稿依時間排序：取「時間 ≤ 目標」的最後一行（最接近又不超過）
    for (const l of lines) {
      if (l.dataset.t !== undefined && Number(l.dataset.t) <= t + 1) target = l;
    }
  }
  if (!target && quote) target = findLineByQuote(lines, quote);
  if (!target) return false;
  target.classList.add("hl");
  // 逐字稿容器自己會捲（結果頁右欄、歷史會議詳情都是）。直接算它的 scrollTop，
  // 不用 scrollIntoView——後者會連整頁一起捲走，把左邊剛點的那張卡片捲不見
  if (container.scrollHeight > container.clientHeight + 1) {
    const delta = target.getBoundingClientRect().top - container.getBoundingClientRect().top;
    container.scrollTo({
      top: container.scrollTop + delta - (container.clientHeight - target.offsetHeight) / 2,
      behavior: "smooth",
    });
  } else {
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  return true;
}

export { TIME_RE, chatHtml, findLineByQuote, jumpToTranscript, parseChatMessages, renderChat, timeLabelToSeconds };
