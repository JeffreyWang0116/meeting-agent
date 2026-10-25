/* ==================================================================
   逐字稿就地編輯：改一句、改講者、合併、拆句、刪一句
   ------------------------------------------------------------------
   畫面上的泡泡是「多行合併」出來的（同講者、沒有時間標記的連續行會併成一句），
   所以泡泡序號對不回原文行號。每個泡泡因此記住自己由原文哪幾行組成（from/to），
   編輯時只換掉那幾行——不把整份逐字稿重新序列化，一個 bug 就賠掉整場會議。

   講者標籤有「繼承」語意：轉錄只在換人時標註，同一人的後續行不重複標。改掉
   某一句的講者（或刪掉某一句）時，沿用它的下一句必須補上明確標籤，否則會被
   一起改掉。這是這個模組最容易出錯、也最值得測的地方。

   純函式、不碰 DOM：tests/test_transcript_edit_js.py 直接用 node 載入測試。
   ================================================================== */
import { matchSpeaker, speakerLabels } from "./speakers.js";

const TIME_RE = /^\[(\d{1,2}(?::\d{1,2}){0,2})\]\s*/;

function renderLine(time, speaker, text) {
  return (time ? `[${time}] ` : "") + (speaker ? `${speaker}：` : "") + text;
}

// 把逐字稿切成泡泡，每個記住來源行範圍。合併規則與 transcript.js 的
// parseChatMessages 一致——畫面上看到幾個泡泡，這裡就要有幾個
function editableBubbles(text, knownNames = []) {
  const raw = String(text || "").split("\n");
  const rows = [];
  raw.forEach((rawLine, index) => {
    let line = rawLine.trim();
    if (!line) return;
    const tm = line.match(TIME_RE);
    if (tm) line = line.slice(tm[0].length).trim();
    if (line) rows.push({ index, time: tm ? tm[1] : null, line });
  });
  const labels = speakerLabels(rows.map(r => r.line), knownNames);
  const bubbles = [];
  let lastSpeaker = null;
  for (const { index, time, line } of rows) {
    const m = matchSpeaker(line, labels);
    const speaker = m ? m.speaker : lastSpeaker;
    if (speaker) lastSpeaker = speaker;
    const content = m ? m.rest : line;
    const last = bubbles[bubbles.length - 1];
    if (!time && last && last.speaker === speaker) {
      last.text += (last.text ? " " : "") + content;
      last.to = index;
      last.lines += 1;
    } else {
      bubbles.push({
        from: index, to: index, time, speaker, text: content,
        // labelled：原文這一行自己標了講者；lines：合併了幾行（譯文對齊要用）
        labelled: Boolean(m), lines: 1,
      });
    }
  }
  return bubbles;
}

// 把 bubbles[index] 的來源行換成 replacement 那幾行，順便處理「下一句沿用講者」
function spliceBubble(text, bubbles, index, replacement) {
  const raw = String(text || "").split("\n");
  const target = bubbles[index];
  const next = bubbles[index + 1];
  // 下一句沒有自己的標籤＝它的講者是從這一句繼承來的。這一句被改掉或刪掉之後，
  // 它就會跟著變——先把它的標籤寫明，釘住原本的講者。next.from 在 target 之後，
  // 所以先改再 splice，索引不會錯位
  if (next && !next.labelled && next.speaker) {
    raw[next.from] = renderLine(next.time, next.speaker, rawBody(raw[next.from]));
  }
  raw.splice(target.from, target.to - target.from + 1, ...replacement);
  return raw.filter(l => l.trim()).join("\n");
}

// 原文某一行去掉時間標記後的內容（講者標籤本來就沒有，所以就是內文）
function rawBody(line) {
  const trimmed = String(line || "").trim();
  const tm = trimmed.match(TIME_RE);
  return tm ? trimmed.slice(tm[0].length).trim() : trimmed;
}

// 改一句的文字或講者。text 全是空白時回 null（留下空行會讓泡泡消失得莫名其妙）
function editBubble(text, index, changes, knownNames = []) {
  const bubbles = editableBubbles(text, knownNames);
  const target = bubbles[index];
  if (!target) return null;
  const body = "text" in changes ? String(changes.text).trim() : target.text;
  if (!body) return null;
  const speaker = "speaker" in changes ? String(changes.speaker || "").trim() : target.speaker;
  return spliceBubble(text, bubbles, index, [renderLine(target.time, speaker, body)]);
}

// 與下一句合併。最後一句沒得合併，原樣回傳
function mergeWithNext(text, index, knownNames = []) {
  const bubbles = editableBubbles(text, knownNames);
  const target = bubbles[index], next = bubbles[index + 1];
  if (!target || !next) return String(text || "");
  const raw = String(text || "").split("\n");
  const merged = renderLine(target.time, target.speaker, `${target.text} ${next.text}`.trim());
  raw.splice(target.from, next.to - target.from + 1, merged);
  return raw.filter(l => l.trim()).join("\n");
}

// 在第 offset 個字處拆成兩句。切在頭尾等於沒切
function splitBubble(text, index, offset, knownNames = []) {
  const bubbles = editableBubbles(text, knownNames);
  const target = bubbles[index];
  if (!target) return String(text || "");
  const head = target.text.slice(0, offset).trim();
  const tail = target.text.slice(offset).trim();
  if (!head || !tail) return String(text || "");
  // 後半沿用同一個講者，所以不重複標籤（與轉錄的慣例一致）；時間標記留給前半，
  // 後半沒有自己的時間——硬掰一個會讓「點重點跳到那一行」跳到錯的位置
  return spliceBubble(text, bubbles, index, [
    renderLine(target.time, target.speaker, head),
    tail,
  ]);
}

// 刪掉一句
function deleteBubble(text, index, knownNames = []) {
  const bubbles = editableBubbles(text, knownNames);
  if (!bubbles[index]) return String(text || "");
  return spliceBubble(text, bubbles, index, []);
}

// 渲染用的形狀。切分規則只有這一份：畫面上第幾個泡泡，就是 editableBubbles 的
// 第幾個，編輯才不會改到別句
function parseChatMessages(text, knownNames = []) {
  return editableBubbles(text, knownNames)
    .map(({ speaker, text: body, time, lines }) => ({ speaker, text: body, time, lines }));
}

export { editBubble, editableBubbles, deleteBubble, mergeWithNext, parseChatMessages, splitBubble };
