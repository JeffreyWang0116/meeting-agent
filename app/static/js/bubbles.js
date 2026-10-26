/* ==================================================================
   逐字稿切泡泡：把一行一行的轉錄結果整理成畫面上看到的一句一句
   ------------------------------------------------------------------
   轉錄出來常常一句一行，照原樣畫會碎成一堆單行。規則是：帶時間標記的行自成
   一句（時間軸不能被合併吃掉），沒有時間標記、同一個講者的連續行併成一句。

   講者標籤有「繼承」語意：轉錄只在換人時標註，同一人的後續行不重複標，
   所以要一路記住上一個講者是誰。

   純函式、不碰 DOM：tests/test_bubbles_js.py 直接用 node 載入測試。
   ================================================================== */
import { matchSpeaker, speakerLabels } from "./speakers.js";

const TIME_RE = /^\[(\d{1,2}(?::\d{1,2}){0,2})\]\s*/;

function parseChatMessages(text, knownNames = []) {
  const raw = String(text || "").split("\n");
  const rows = [];
  for (const rawLine of raw) {
    let line = rawLine.trim();
    if (!line) continue;
    const tm = line.match(TIME_RE);
    if (tm) line = line.slice(tm[0].length).trim();
    if (line) rows.push({ time: tm ? tm[1] : null, line });
  }
  const labels = speakerLabels(rows.map(r => r.line), knownNames);
  const bubbles = [];
  let lastSpeaker = null;
  for (const { time, line } of rows) {
    const m = matchSpeaker(line, labels);
    const speaker = m ? m.speaker : lastSpeaker;
    if (speaker) lastSpeaker = speaker;
    const content = m ? m.rest : line;
    const last = bubbles[bubbles.length - 1];
    if (!time && last && last.speaker === speaker) {
      last.text += (last.text ? " " : "") + content;
      last.lines += 1;
    } else {
      // lines：合併了幾行。譯文對齊要用——譯文沒有時間標記，只能靠行數分組
      bubbles.push({ time, speaker, text: content, lines: 1 });
    }
  }
  return bubbles;
}

export { parseChatMessages };
