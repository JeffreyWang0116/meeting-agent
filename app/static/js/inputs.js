import { api } from "./api.js";
import { $, clearError, esc, showError, showNotice } from "./core.js";
import { hideResultSkeleton, markAnalysisStart, renderResult, showResultSkeleton } from "./result.js";
import { chunkSeconds, correctTypos, meetingTerms, micConstraints, nameSpeakers, populateMicDevices, selectedFeatures, sysSourceValue, wantSystemAudio } from "./setup.js";
import { chatHtml, renderChat } from "./transcript.js";

/* ==================================================================
   9. 輸入路徑：純文字貼上、檔案上傳（含拖曳）、即時聆聽
   ================================================================== */
// ---- 路徑 1：純文字 ----
$("btnAnalyzeText").addEventListener("click", async () => {
  clearError();
  const btn = $("btnAnalyzeText");
  const original = btn.innerHTML;
  btn.disabled = true; btn.textContent = "AI 分析中…";
  markAnalysisStart();
  showResultSkeleton();
  try {
    const result = await api.analyze({
        text: $("textInput").value,
        meeting_date: $("meetingDate").value || null,
        kind: $("meetingKind").value,
        features: selectedFeatures(),
        correct_typos: correctTypos(),
        name_speakers: nameSpeakers(),
        terms: meetingTerms(),
    });
    renderResult(result, $("textInput").value);
  } catch (e) { hideResultSkeleton(); showError(e.message); }
  finally { btn.disabled = false; btn.innerHTML = original; }
});

// ---- 路徑 2：檔案上傳 ----
const JOB_STATUS_ZH = {
  queued: "排隊中…", extracting: "從影片抽取聲音軌…", transcribing: "轉錄中",
  analyzing: "AI 分析中…", done: "完成", error: "失敗",
};

$("btnUpload").addEventListener("click", async () => {
  clearError();
  const file = $("fileInput").files[0];
  if (!file) { showError("請先選擇檔案"); return; }

  const btn = $("btnUpload");
  btn.disabled = true;
  markAnalysisStart();
  $("fileProgress").style.display = "block";
  $("fileTranscript").style.display = "block";
  $("fileTranscript").textContent = "";

  try {
    const form = new FormData();
    form.append("file", file);
    if ($("meetingDate").value) form.append("meeting_date", $("meetingDate").value);
    form.append("kind", $("meetingKind").value);
    form.append("terms", JSON.stringify(meetingTerms()));
    const features = selectedFeatures();
    if (features !== null) form.append("features", features.join(","));
    if (correctTypos()) form.append("correct_typos", "true");
    if (nameSpeakers()) form.append("name_speakers", "true");
    const { job_id } = await api.uploadMedia(form);

    let lastPct = -1, stalled = 0;
    while (true) {
      await new Promise(r => setTimeout(r, 1500));
      const job = await api.mediaJob(job_id);
      const pct = Math.round((job.progress || 0) * 100);
      $("fileProgress").firstElementChild.style.width = pct + "%";
      // 卡在同一個百分比超過兩輪（~3 秒）就切成「處理中」樣式：長段落轉錄時
      // 進度本來就會不動好一陣子，靜止的進度條會讓人以為當掉了
      const transcribing = job.status === "transcribing";
      stalled = transcribing && pct === lastPct ? stalled + 1 : 0;
      lastPct = pct;
      const stuck = stalled >= 2;
      $("fileProgress").classList.toggle("stuck", stuck);
      $("fileStatus").innerHTML =
        esc(JOB_STATUS_ZH[job.status] + (transcribing ? `（${pct}%）` : "")) +
        (stuck ? '<span class="spinner" aria-hidden="true"></span>' : "");
      if (job.transcript) {
        renderChat($("fileTranscript"), job.transcript);
        $("fileTranscript").scrollTop = $("fileTranscript").scrollHeight;
      }
      // 轉錄階段的進度條與逐字稿在「新會議」畫面，看得到才有意義；
      // 進到分析階段才切去結果頁擺骨架
      if (job.status === "analyzing") showResultSkeleton();
      if (job.status === "done") { renderResult(job.result, job.transcript); break; }
      if (job.status === "error") throw new Error(job.error || "轉錄失敗");
    }
  } catch (e) { hideResultSkeleton(); showError(e.message); $("fileStatus").textContent = "失敗"; }
  finally { btn.disabled = false; }
});

// ---- 拖曳上傳 ----
(function () {
  const zone = $("dropZone");
  ["dragenter", "dragover"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove("dragover"); }));
  zone.addEventListener("drop", e => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    $("fileInput").files = files;
    $("fileStatus").textContent = `已選擇：${files[0].name}`;
  });
})();

// ---- 路徑 3：即時聆聽 ----
// liveStream 是實際交給 MediaRecorder 錄的那條軌：只錄麥克風時就是麥克風串流本身；
// 若同時收系統／耳機音源，則是「麥克風＋系統音源」混音後的輸出。liveMicStream／
// liveSysStream 保留原始來源，結束時要各自關掉裝置；liveMixCtx 是負責混音的 AudioContext。
let liveStream = null, liveRecorder = null, liveSessionId = null;
let liveMicStream = null, liveSysStream = null, liveMixCtx = null;
let liveRecording = false, liveSegTimer = null, uploadsInFlight = 0, liveStartTime = null, liveTickTimer = null;
// 目前在錄的錄音器。相鄰兩段刻意重疊幾秒，所以重疊期間會同時有兩個
let liveRecorders = [];
let liveSegIndex = 0, liveSentCount = 0, liveWakeLock = null, liveStarting = false;

// 取得要錄的串流。withSystemAudio 為真時，額外抓耳機／系統音源（對方的聲音），
// 和麥克風混成一條軌一起錄。音源來源由 sysSourceValue() 決定：
//   deviceId → 直接用 getUserMedia 錄該回放裝置（立體聲混音等），免跳分享視窗；
//   "display" → 用 getDisplayMedia 分享畫面擷取（相容性最高，但每次會跳分享視窗）。
// 常見「App 內建瀏覽器」的 UA 特徵。這些 webview（Android 上尤其）多半直接封鎖
// 麥克風、連權限詢問都不跳，症狀就是「按了開始聆聽卻什麼都沒發生」。要引導使用者
// 改用系統瀏覽器（Chrome / Safari）開啟才拿得到麥克風。
function inAppBrowserName() {
  const ua = navigator.userAgent || "";
  if (/\bLine\//i.test(ua)) return "LINE";
  if (/FBAN|FBAV|FB_IAB/i.test(ua)) return "Facebook";
  if (/Instagram/i.test(ua)) return "Instagram";
  if (/\bMessenger\b/i.test(ua)) return "Messenger";
  if (/MicroMessenger/i.test(ua)) return "WeChat";
  return null;
}

function openInBrowserHint() {
  const app = inAppBrowserName();
  if (!app) return "";
  return `你正在 ${app} 的內建瀏覽器裡開啟，這類瀏覽器通常直接封鎖麥克風（所以不會跳出權限詢問）。`
    + "請點畫面右上角的「⋯」選單，選「用其他瀏覽器開啟」，或把網址複製到 Chrome 再試。";
}

// 呼叫 getUserMedia 前的前置檢查：內建瀏覽器／非 https／不支援 getUserMedia 這三種情況，
// 手機上要嘛連權限詢問都不跳，要嘛丟出來的原生錯誤訊息看不懂，先擋下給可操作的說明。
// 回傳錯誤訊息字串；沒問題則回傳 null。
function micAvailabilityError() {
  const inAppMsg = openInBrowserHint();
  if (inAppMsg) return inAppMsg;
  if (!window.isSecureContext)
    return "麥克風只在 https:// 加密連線（或 localhost）下可用。請用 https:// 開頭的網址開啟再試。";
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia)
    return "這個環境取不到麥克風。iPhone 請直接用 Safari 開啟網址（有些情況下從主畫面捷徑／內嵌瀏覽器開啟會擋掉麥克風），Android 請用 Chrome 開啟。";
  return null;
}

// getUserMedia 的錯誤依類型給可操作說明。內建瀏覽器是根因就先講它；否則手機上
// 最常見的是「之前拒絕過就不再自動跳詢問」，只回一句籠統的話會讓人不知道去哪開。
function micPermissionMessage(e) {
  const inApp = openInBrowserHint();
  if (inApp) return inApp;
  const name = (e && e.name) || "";
  if (name === "NotAllowedError" || name === "SecurityError")
    return "麥克風權限被拒或尚未允許。手機一旦拒絕過就不會再自動跳出詢問，要手動開："
      + "iPhone 到「設定 → Safari → 麥克風」或點網址列左側的「ㄗA」圖示改成允許；"
      + "Android Chrome 點網址列的鎖頭 → 權限 → 麥克風，改成允許後重新整理，再按一次「開始聆聽」。";
  if (name === "NotFoundError" || name === "OverconstrainedError")
    return "找不到可用的麥克風。請確認手機麥克風沒有被停用或被系統佔用。";
  if (name === "NotReadableError")
    return "麥克風正被其他 App 佔用（通話、錄音、相機等）。關掉那個 App 再回來重試。";
  return "無法取得麥克風：" + ((e && e.message) || e) + "（請在瀏覽器／系統設定允許此網站使用麥克風）";
}

async function buildLiveStream(withSystemAudio) {
  try {
    // 與錄樣本同一支麥克風：換裝置錄的樣本，音色差距足以讓聲紋比對失效
    liveMicStream = await navigator.mediaDevices.getUserMedia(micConstraints());
  } catch (e) {
    throw new Error(micPermissionMessage(e));
  }
  if (!withSystemAudio) return liveMicStream;

  liveSysStream = await acquireSystemAudio(sysSourceValue());

  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) throw new Error("此瀏覽器不支援 Web Audio，無法混合系統音源");
  liveMixCtx = new Ctx();
  if (liveMixCtx.state === "suspended") liveMixCtx.resume().catch(() => {});
  const dest = liveMixCtx.createMediaStreamDestination();
  liveMixCtx.createMediaStreamSource(liveMicStream).connect(dest);
  liveMixCtx.createMediaStreamSource(liveSysStream).connect(dest);
  return dest.stream;
}

// 依所選來源取得耳機／系統音源串流；接上時提示、失敗時丟出可讀的錯誤。
async function acquireSystemAudio(source) {
  // 直接擷取回放裝置（立體聲混音／虛擬音效線）——關掉麥克風用的回音消除等處理，避免破壞系統音源
  if (source && source !== "display") {
    let sys;
    try {
      sys = await navigator.mediaDevices.getUserMedia({
        audio: { deviceId: { exact: source }, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
    } catch (e) {
      throw new Error("無法開啟所選的系統音源裝置：" + e.message + "。請改選其他來源，或在下拉選單改用「分享畫面擷取」");
    }
    const t = sys.getAudioTracks()[0];
    console.log("[live] 已接上系統音源裝置：", t && t.label);
    showNotice("已接上耳機／系統音源（" + ((t && t.label) || "所選裝置") + "），對方的聲音會一起錄進逐字稿。");
    return sys;
  }

  // 分享畫面擷取
  if (!navigator.mediaDevices.getDisplayMedia) {
    throw new Error("此瀏覽器不支援分享畫面擷取（此功能僅桌機版 Chrome／Edge 可用），請取消勾選「同時收錄耳機／系統音源」");
  }
  let sys;
  try {  // 分享對話框一定要挑一個畫面來源才會給音訊，所以連 video 一起要。
    sys = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
  } catch (e) {
    throw new Error("未取得系統音源分享（已取消或被拒）：" + e.message);
  }
  const sysAudio = sys.getAudioTracks()[0];
  if (!sysAudio) {
    sys.getTracks().forEach(t => t.stop());
    throw new Error("這次分享沒有帶到聲音。桌面 App 開會請選「整個螢幕」、會議在瀏覽器分頁請選該「分頁」，並務必勾選「分享系統音訊／分頁音訊」再試一次");
  }
  // 關鍵：不要 stop 掉畫面軌！系統音源的擷取綁在這個螢幕分享 session 上，
  // 一旦停掉畫面，聲音會跟著斷（症狀就是「只錄到麥克風」）。改成把畫面「停用」
  // ——產生黑畫面、幾乎不吃資源，但 session 保持存活，聲音才會持續進來。
  sys.getVideoTracks().forEach(t => { t.enabled = false; });
  console.log("[live] 已接上系統音源（分享畫面）：", sysAudio.label || "(未命名)", "muted=", sysAudio.muted);
  showNotice("已接上系統／耳機音源，對方的聲音會一起錄進逐字稿。請保持螢幕分享開著，不要按瀏覽器的「停止分享」。");
  sysAudio.addEventListener("ended", () => {  // 使用者按「停止分享」時提醒接下來只剩麥克風
    if (liveRecording) showNotice("螢幕／系統音源分享已停止，接下來只會錄到麥克風。");
  });
  return sys;
}

// 關掉聆聽用到的所有音訊來源與混音器（麥克風、系統音源、混音 AudioContext）
function releaseLiveStreams() {
  for (const s of [liveMicStream, liveSysStream, liveStream]) {
    if (s) s.getTracks().forEach(t => t.stop());
  }
  if (liveMixCtx) liveMixCtx.close().catch(() => {});
  liveMicStream = liveSysStream = liveStream = liveMixCtx = null;
}

// 手機螢幕熄滅會讓瀏覽器暫停錄音 → 聆聽期間用 Wake Lock 保持螢幕常亮
async function acquireWakeLock() {
  if (!("wakeLock" in navigator)) return;
  try { liveWakeLock = await navigator.wakeLock.request("screen"); } catch (e) { /* 被拒僅代表螢幕可能自動熄滅 */ }
}
function releaseWakeLock() {
  if (liveWakeLock) { try { liveWakeLock.release(); } catch (e) {} liveWakeLock = null; }
}
document.addEventListener("visibilitychange", () => {
  if (!liveRecording || document.visibilityState !== "visible") return;
  acquireWakeLock();  // 切回前景時螢幕鎖會被系統釋放，要重新取得
  if (!liveRecorders.length) recordSegment();  // 錄音若被系統中斷則自動接續
});

function pickMime() {
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return "";
}

function liveTick() {
  if (!liveRecording) return;
  const s = Math.floor((Date.now() - liveStartTime) / 1000);
  const clock = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  const pending = uploadsInFlight > 0 ? `，${uploadsInFlight} 段辨識中…` : "";
  const sent = liveSentCount > 0 ? `已送出 ${liveSentCount} 段${pending}` : `第一段約 ${Math.min(12, chunkSeconds)} 秒後送出`;
  $("liveStatus").innerHTML = `<span class="rec-dot"></span>聆聽中 ${clock}（${sent}）`;
  if (!$("liveStage").hidden) {
    $("stageTime").textContent = clock;
    $("stageNote").innerHTML = `<span class="rec-dot"></span>${sent}`;
  }
}

// ---- 聆聽沉浸畫面：聲控光球＋計時＋即時字幕 ----
// 音量分析共用 liveStream（MediaRecorder 錄的同一份），不另外開麥克風：
// 手機上開第二份串流可能跟錄音搶裝置，也會多跳一次權限。
let orb = null, orbRaf = 0, audioCtx = null, analyser = null, analyserData = null;
let transcriptHome = null;  // 逐字稿節點原本的位置，收合時要放回去

function initStageAudio() {
  if (analyser || !liveStream) return;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  try {
    audioCtx = new Ctx();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.3;
    // 只接到 analyser、刻意不接 destination——接上輸出等於把麥克風擴音出來，會回授
    audioCtx.createMediaStreamSource(liveStream).connect(analyser);
    analyserData = new Uint8Array(analyser.frequencyBinCount);
  } catch (e) {
    audioCtx = null; analyser = null;  // 分析失敗不影響錄音，光球退化成只自轉
  }
}

function releaseStageAudio() {
  if (audioCtx) audioCtx.close().catch(() => {});
  audioCtx = null; analyser = null; analyserData = null;
}

function pumpLevel() {
  orbRaf = requestAnimationFrame(pumpLevel);
  if (!orb || !analyser) return;
  analyser.getByteFrequencyData(analyserData);
  let sum = 0;
  for (let i = 0; i < analyserData.length; i++) {
    const v = analyserData[i] / 255;
    sum += v * v;
  }
  orb.setLevel(Math.sqrt(sum / analyserData.length) * 4.5);  // RMS 比平均值更貼近人聲強弱
}

function openStage() {
  $("liveStage").hidden = false;
  $("btnLiveStage").style.display = "none";

  // 把逐字稿「整個節點」搬進沉浸畫面（不是複製），現有字幕附加邏輯才不用改
  const box = $("liveTranscript");
  if (!transcriptHome) transcriptHome = { parent: box.parentNode, next: box.nextSibling };
  $("stageSlot").appendChild(box);

  const holder = $("stageOrb");
  orb = window.createVoiceOrb ? window.createVoiceOrb(holder) : null;
  holder.classList.toggle("no-webgl", !orb);  // 沒有 WebGL 就退回 CSS 呼吸光暈

  initStageAudio();
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  cancelAnimationFrame(orbRaf);
  pumpLevel();
  liveTick();
}

// 只收合畫面，錄音與上傳完全不受影響
function closeStage() {
  if ($("liveStage").hidden) return;
  $("liveStage").hidden = true;
  cancelAnimationFrame(orbRaf);
  orbRaf = 0;
  if (orb) { orb.destroy(); orb = null; }
  const box = $("liveTranscript");
  if (transcriptHome) {
    transcriptHome.parent.insertBefore(box, transcriptHome.next);
    transcriptHome = null;
  }
}

$("btnStageStop").addEventListener("click", () => $("btnLiveStop").click());
$("btnStageClose").addEventListener("click", () => {
  closeStage();
  if (liveRecording) $("btnLiveStage").style.display = "inline-flex";
});
$("btnLiveStage").addEventListener("click", openStage);

// 即時字幕流：逐段附加逐字稿行（後端回傳的文字已帶整場時間戳），末端保留打字游標。
// liveSpeakers 讓同一位講者在整場聆聽中維持同色。
let liveSpeakers = {};
// 逐字稿原文（非 HTML）。伺服器重啟會讓聆聽 session 連同它累積的逐字稿一起消失，
// 這份瀏覽器端的副本是那時唯一還救得回來的內容，見 finishLiveSession 的 404 退路。
// 取後端每段回傳的完整 transcript 而不是自己串接：段落可能亂序辨識完，
// 後端是依錄音順序的 index 填槽，它的版本才是對的。
let liveTranscriptText = "";

function appendCaption(text, translation) {
  const caret = $("liveCaret");
  if (!caret) return;
  caret.insertAdjacentHTML("beforebegin", chatHtml(text, liveSpeakers, translation));
  $("liveTranscript").scrollTop = $("liveTranscript").scrollHeight;
}

// 上傳失敗的音訊段。原本失敗就直接丟掉，那 45 秒永久消失、錄音還在繼續，
// 使用者多半不會發現中間缺了一段——桌機 wifi 很少踩到，手機網路是家常便飯。
// 記憶體上限：一段 opus 約數百 KB，超過就停止累積並明講，總比整台當掉好。
const MAX_PENDING_CHUNKS = 24;
const RETRY_DELAYS_MS = [2000, 5000, 12000];
let pendingChunks = [];
let droppedChunks = 0;

const sleep = ms => new Promise(r => setTimeout(r, ms));

// 400/404 這類「再送幾次也是一樣」的錯不該重試：session 已經死了，
// 或這段音訊後端根本不收，重試只會拖慢結束流程
const worthRetrying = err => !(err.status >= 400 && err.status < 500);

// 已經當場回報過「認出某某人」的名字，同一個人不重複打擾
const announcedNames = new Set();

// 預錄有沒有生效，開完一小時才知道就太晚了。後端在前幾段之後會先比對一次，
// 結果隨當段回傳，這裡當場講出來——來得及重錄樣本，或至少心裡有底
function noteRecognisedNames(names) {
  const fresh = Object.values(names || {}).filter(n => n && !announcedNames.has(n));
  if (!fresh.length) return;
  fresh.forEach(n => announcedNames.add(n));
  showNotice(`已用預錄的聲音樣本認出：${fresh.join("、")}。結束分析時會把逐字稿的講者代號換成姓名。`);
}

async function postLiveChunk(blob, offsetSeconds, overlapSeconds) {
  const ext = blob.type.includes("mp4") ? ".mp4" : ".webm";
  const form = new FormData();
  form.append("file", blob, "chunk" + ext);
  // 本段在整場會議中的開始秒數：後端把段內相對時間戳平移成整場時間
  if (offsetSeconds != null) form.append("offset", offsetSeconds);
  // 本段開頭與前一段重疊了幾秒，後端據此濾掉被轉錄兩次的那幾行
  if (overlapSeconds) form.append("overlap", overlapSeconds);
  const r = await api.liveChunk(liveSessionId, form);
  if (r.transcript) liveTranscriptText = r.transcript;
  if (r.text) appendCaption(r.text, r.translation);
  noteRecognisedNames(r.names);
}

async function uploadLiveChunk(blob, offsetSeconds, overlapSeconds) {
  // uploadsInFlight 要涵蓋整個重試過程：結束會議時會等它歸零才送出分析，
  // 不然重試中的那一段就趕不上，等於還是掉了
  uploadsInFlight++;
  liveSentCount++;
  try {
    for (let attempt = 0; ; attempt++) {
      try {
        await postLiveChunk(blob, offsetSeconds, overlapSeconds);
        return;
      } catch (e) {
        if (attempt >= RETRY_DELAYS_MS.length || !worthRetrying(e)) {
          // 還沒放棄：排進佇列，結束會議前會再試一輪
          if (pendingChunks.length < MAX_PENDING_CHUNKS) {
            pendingChunks.push({ blob, offsetSeconds, overlapSeconds });
          } else {
            droppedChunks++;
          }
          return;
        }
        await sleep(RETRY_DELAYS_MS[attempt]);
      }
    }
  } finally { uploadsInFlight--; }
}

// 結束會議前的最後一次補送。此時網路多半已經恢復，成功率比錄音當下高。
// 回傳仍然失敗的段數，讓呼叫端決定要不要警告使用者。
async function flushPendingChunks() {
  if (!pendingChunks.length) return 0;
  $("liveStatus").textContent = `補送 ${pendingChunks.length} 段稍早失敗的錄音…`;
  const queue = pendingChunks;
  pendingChunks = [];
  const stillFailed = [];
  for (const c of queue) {
    try { await postLiveChunk(c.blob, c.offsetSeconds, c.overlapSeconds); }
    catch (e) { stillFailed.push(c); }
  }
  return stillFailed.length + droppedChunks;
}

// 相鄰兩段刻意重疊幾秒：下一段的錄音器提早這麼久開始錄，一句話才不會被
// 硬切點剁成兩半（stop 與 start 之間還有幾十毫秒是真的沒錄到）。重疊那幾秒
// 會被轉錄兩次，後端依絕對時間濾掉（見 /api/live/{id}/chunk 的 overlap）。
// 模型也因此聽得到前一段的聲音，跨段沿用同一組講者標籤才有依據。
const LIVE_OVERLAP_SECONDS = 3;
// 同一份串流同時掛兩個 MediaRecorder，絕大多數瀏覽器沒問題，但不是規格保證。
// 真的不行時要能退回「停了才開下一段」的老路——重疊只是加分，賠掉整段錄音
// 就本末倒置了。偵測到一次失敗就整場不再重疊
let overlapWorks = true;

// 每段用「新的 MediaRecorder」錄，確保每段都有完整檔頭、可獨立解碼。
// liveStarting 旗標＋「已在錄就不重啟」的檢查，避免 onstop 與 visibilitychange
// 同時觸發時建立兩個錄音器造成段落重複。
// scheduled=true 代表這是「重疊計時器」排定的下一段：此時本段仍在錄，
// 正是預期中的重疊，不可以被「已在錄就不重啟」的守衛擋掉。其餘呼叫者
// （中斷後補接、切回前景）則維持原本的守衛，避免同一段被錄兩次
function recordSegment(scheduled) {
  if (!liveRecording || liveStarting) return;
  if (!scheduled && liveRecorders.some(r => r.state === "recording")) return;
  liveStarting = true;
  const chunks = [];
  const mime = pickMime();
  let recorder;
  try {
    recorder = new MediaRecorder(liveStream, mime ? { mimeType: mime } : undefined);
  } catch (e) {
    // liveStarting 卡在 true 的話，之後每一次 recordSegment 都會直接 return——
    // 錄音就此靜悄悄地停住，而且完全沒有錯誤訊息
    liveStarting = false;
    if (scheduled) { overlapWorks = false; return; }  // 老路（onstop）會接手
    showError("無法開始錄音：" + (e.message || e));
    return;
  }
  liveRecorder = recorder;
  liveRecorders.push(recorder);
  const segStart = Math.floor((Date.now() - liveStartTime) / 1000);  // 本段在整場中的開始秒數
  // 只有「重疊計時器排定的下一段」開頭才真的與前一段重疊。第一段沒有前段，
  // 中斷後補接的那一段也沒有（前一段早就停了）——那兩種情況記成 3 秒的話，
  // 後端會把開頭三秒當成重複刪掉，那幾句就真的消失了
  const overlap = scheduled ? LIVE_OVERLAP_SECONDS : 0;
  recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  recorder.onerror = e => showError("錄音發生錯誤：" + ((e.error && e.error.message) || "未知原因"));
  recorder.onstop = () => {
    clearTimeout(recorder.stopTimer);
    clearTimeout(recorder.nextTimer);
    liveRecorders = liveRecorders.filter(r => r !== recorder);
    // 正常情況下，下一段早在重疊計時器裡就開錄了；這裡是錄音被系統中斷
    // （來電、切到背景）之後的補接，不然整場會就此靜悄悄地停住
    if (liveRecording && !liveRecorders.length) recordSegment();
    const blob = new Blob(chunks, { type: recorder.mimeType });
    if (blob.size > 0) {
      uploadLiveChunk(blob, segStart, overlap);
    } else if (overlap) {
      // 排程開的那一顆錄出空白：同上，之後不再重疊（這一段已經賠掉了）
      overlapWorks = false;
      console.log("[live] 重疊錄音錄到空白，改回不重疊");
    }
  };
  try {
    recorder.start();
  } catch (e) {
    liveStarting = false;
    liveRecorders = liveRecorders.filter(r => r !== recorder);
    if (scheduled) { overlapWorks = false; return; }
    showError("無法開始錄音：" + (e.message || e));
    return;
  }
  liveStarting = false;
  // 排程開的那一顆沒真的錄起來＝這個瀏覽器不接受兩個錄音器並存。當場收掉它，
  // 讓前一段的 onstop 照老路接續，否則這 45 秒會整段消失
  if (scheduled && recorder.state !== "recording") {
    overlapWorks = false;
    liveRecorders = liveRecorders.filter(r => r !== recorder);
    console.log("[live] 這個瀏覽器不支援重疊錄音，改回不重疊");
    return;
  }
  // 第一段縮短到 12 秒：讓使用者快速看到第一句逐字稿，確認「真的有在聽」
  const secs = liveSegIndex === 0 ? Math.min(12, chunkSeconds) : chunkSeconds;
  liveSegIndex++;
  // 下一段提早 LIVE_OVERLAP_SECONDS 開錄，與本段重疊。退回不重疊模式時就不排，
  // 改由本段的 onstop 接續（＝這個功能出現之前的行為）
  if (overlapWorks) {
    recorder.nextTimer = setTimeout(
      () => { if (liveRecording) recordSegment(true); },
      Math.max(1, secs - LIVE_OVERLAP_SECONDS) * 1000
    );
  }
  recorder.stopTimer = liveSegTimer = setTimeout(() => {
    if (recorder.state !== "inactive") recorder.stop();
  }, secs * 1000);
}

// ---- 預錄聲音辨識人（選用功能）----
// 會前請每位與會者各錄一小段話（或改上傳一段既有音檔），按下「開始聆聽」時
// 在背景送到後端；聆聽到第二段左右就會先比對一次，結束分析時把逐字稿的
// 「講者A/B/C」換成真實姓名。
//
// 沒勾這個功能就完全不執行：不開麥克風、不上傳、不多打任何 API，整條路徑與
// 這個功能不存在時相同。錄音失敗或上傳失敗也一律只降級成「維持講者代號」，
// 絕不擋住聆聽本身——會議內容遠比名字重要。
//
// 限制：錄樣本只錄得到本機麥克風。線上會議中「在對面」的人請改用「用音檔」，
// 上傳一段他的語音訊息當樣本。
const ENROLL_SECONDS = 10;  // 太短聲紋特徵不足、太長浪費額度；實測 10 秒足夠
let enrollPeople = [];      // 依序對應畫面上每一列：{ name, blob }
let enrollBusy = false;     // 同時只錄一個人，避免兩列搶同一支麥克風
// 背景上傳中的樣本。開始聆聽時不等它（等於白掉開場那幾秒），但結束分析前
// 一定要等到——沒送達的樣本比對不到，那個人就白錄了
let enrollUploads = null;

function enrollOn() {
  return $("liveEnrollOn").checked;
}

function syncEnrollPeople() {
  const n = Number($("liveEnrollCount").value || 0);
  while (enrollPeople.length < n) enrollPeople.push({ name: "", blob: null });
  enrollPeople.length = n;  // 人數調少時，多出來的樣本一併丟掉
}

// ---- 樣本品質把關 ----
// 不能用的樣本要當場擋下來。裝置選錯、麥克風被靜音、離太遠、或整整 10 秒
// 只在最後講了一句——這些樣本一樣有檔案大小，光看 blob.size 看不出來；
// 使用者會一路開完整場會，直到分析結果沒有半個名字才發現白錄。
const ENROLL_SPEECH_RMS = 0.02;       // 單框 RMS 高於此值算「這一瞬間有人在講話」
const ENROLL_MIN_SPEECH_RATIO = 0.25; // 有聲時間要佔四分之一以上，嗓音特徵才夠
const ENROLL_MIN_SECONDS = 3;         // 再短的樣本比對不出東西
const ENROLL_CLIP_RATIO = 0.01;       // 破音超過 1%：削平的波形一樣比不準

// 回傳 { ok, reason }。測不了（瀏覽器解不了這個編碼）就一律放行，交給後端
// 比對去判斷——寧可漏擋，也不要擋掉其實可用的樣本
async function checkSample(blob) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return { ok: true };
    const ctx = new Ctx();
    const buf = await ctx.decodeAudioData(await blob.arrayBuffer());
    ctx.close();
    if (buf.duration < ENROLL_MIN_SECONDS) return { ok: false, reason: "太短" };
    const data = buf.getChannelData(0);
    const frame = Math.max(1, Math.round(buf.sampleRate * 0.025));  // 25ms 一框
    let frames = 0, speech = 0, clipped = 0;
    for (let i = 0; i + frame <= data.length; i += frame) {
      let sum = 0;
      for (let j = i; j < i + frame; j++) {
        sum += data[j] * data[j];
        if (Math.abs(data[j]) > 0.99) clipped++;
      }
      frames++;
      if (Math.sqrt(sum / frame) > ENROLL_SPEECH_RMS) speech++;
    }
    if (!frames) return { ok: true };
    // 整段平均音量看不出「只在最後講了一句」——那種樣本平均值一樣過關，
    // 但可用的嗓音只有一兩秒。改看「有聲音的時間佔多少」
    const ratio = speech / frames;
    if (ratio < 0.05) return { ok: false, reason: "幾乎沒有聲音" };
    if (ratio < ENROLL_MIN_SPEECH_RATIO) return { ok: false, reason: "說話時間太少" };
    if (clipped / data.length > ENROLL_CLIP_RATIO) return { ok: false, reason: "破音" };
    return { ok: true };
  } catch (e) {
    return { ok: true };  // 這個瀏覽器解不了這個編碼就不擋
  }
}

// 把一段樣本（錄的或上傳的）收進某一列，順便驗品質
async function acceptSample(index, blob) {
  const person = enrollPeople[index];
  if (!person || !blob || !blob.size) return;
  person.blob = blob;
  const verdict = await checkSample(blob);
  person.bad = verdict.ok ? "" : verdict.reason;
  renderEnrollRows();
}

// ---- 音量表 ----
// 錄之前就看得到「這支麥克風現在收到多大聲」，比錄完再驗屍早得多。
// 測試鈕與錄音中共用同一組元件
let meterCtx = null, meterRaf = 0, meterStream = null;

function startMeter(stream, ownStream) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  stopMeter();
  try {
    meterCtx = new Ctx();
    meterStream = ownStream ? stream : null;  // 自己開的串流才由自己關掉
    const analyser = meterCtx.createAnalyser();
    analyser.fftSize = 512;
    meterCtx.createMediaStreamSource(stream).connect(analyser);
    const data = new Uint8Array(analyser.frequencyBinCount);
    const bar = $("liveEnrollMeter");
    const pump = () => {
      meterRaf = requestAnimationFrame(pump);
      analyser.getByteFrequencyData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += (data[i] / 255) * (data[i] / 255);
      const level = Math.min(1, Math.sqrt(sum / data.length) * 4.5);
      bar.style.width = Math.round(level * 100) + "%";
      bar.classList.toggle("ok", level > 0.18);  // 到得了「聽得清楚」才轉綠
    };
    pump();
  } catch (e) {
    meterCtx = null;  // 音量表壞掉不影響錄音
  }
}

function stopMeter() {
  cancelAnimationFrame(meterRaf);
  meterRaf = 0;
  if (meterCtx) meterCtx.close().catch(() => {});
  meterCtx = null;
  if (meterStream) meterStream.getTracks().forEach(t => t.stop());
  meterStream = null;
  const bar = $("liveEnrollMeter");
  bar.style.width = "0%";
  bar.classList.remove("ok");
}

$("btnEnrollMicTest").addEventListener("click", async () => {
  if (enrollBusy || liveRecording) return;
  const availErr = micAvailabilityError();
  if (availErr) { showError(availErr); return; }
  const hint = $("liveEnrollMeterHint");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia(micConstraints());
  } catch (e) {
    showError("無法使用麥克風：" + micPermissionMessage(e));
    return;
  }
  populateMicDevices(false);  // 授權後才拿得到裝置名稱，順手補進下拉選單
  hint.textContent = "說說看，綠色代表音量足夠…";
  startMeter(stream, true);
  setTimeout(() => {
    stopMeter();
    hint.textContent = "測試結束。音量太小就靠近一點，或換一支麥克風。";
  }, 8000);
});

// 試聽：錄完只寫「已錄好」的話，使用者沒辦法確認到底錄到了什麼
function playEnrollment(index) {
  const p = enrollPeople[index];
  if (!p || !p.blob) return;
  const url = URL.createObjectURL(p.blob);
  const audio = new Audio(url);
  const cleanup = () => URL.revokeObjectURL(url);
  audio.onended = cleanup;
  audio.onerror = () => { cleanup(); showError("無法播放這段樣本，建議重錄。"); };
  audio.play().catch(() => { cleanup(); showError("無法播放這段樣本，建議重錄。"); });
}

function clearEnrollment(index) {
  const p = enrollPeople[index];
  if (!p) return;
  p.blob = null;
  p.bad = "";
  renderEnrollRows();
}

function renderEnrollRows() {
  syncEnrollPeople();
  $("liveEnrollList").innerHTML = enrollPeople.map((p, i) => `
    <div class="enroll-row${p.blob ? " done" : ""}${p.bad ? " silent" : ""}">
      <span class="enroll-no">${i + 1}.</span>
      <input type="text" id="enrollName${i}" maxlength="20"
             placeholder="第 ${i + 1} 位的姓名" value="${esc(p.name)}">
      <button class="ghost" type="button" id="enrollRec${i}">
        <svg class="i-sm" aria-hidden="true"><use href="/static/icons.svg#mic"/></svg>
        ${p.blob ? "重錄" : "錄音"}
      </button>
      <button class="ghost" type="button" id="enrollPick${i}" title="改用既有音檔當樣本：線上會議中在對面的人，可以傳一段語音訊息給你">用音檔</button>
      <input type="file" class="enroll-file" id="enrollFile${i}" accept="audio/*">
      ${p.blob ? `
      <button class="ghost" type="button" id="enrollPlay${i}" title="試聽這段樣本">試聽</button>
      <button class="ghost" type="button" id="enrollDel${i}" title="刪掉這段樣本">清除</button>` : ""}
      <span class="enroll-state" id="enrollState${i}">${
        p.bad ? esc(p.bad) : p.blob ? "已錄好" : "未錄"
      }</span>
    </div>`).join("");
  enrollPeople.forEach((p, i) => {
    // 姓名寫回資料模型，重繪（換人數、錄完音）時才不會把使用者打的字弄丟
    $(`enrollName${i}`).addEventListener("input", e => { p.name = e.target.value; });
    $(`enrollRec${i}`).addEventListener("click", () => recordEnrollment(i));
    $(`enrollPick${i}`).addEventListener("click", () => $(`enrollFile${i}`).click());
    $(`enrollFile${i}`).addEventListener("change", e => {
      const file = e.target.files && e.target.files[0];
      if (file) acceptSample(i, file);
    });
    if (p.blob) {
      $(`enrollPlay${i}`).addEventListener("click", () => playEnrollment(i));
      $(`enrollDel${i}`).addEventListener("click", () => clearEnrollment(i));
    }
  });
  const ready = enrollPeople.filter(p => p.name.trim() && p.blob).length;
  const bad = enrollPeople.filter(p => p.blob && p.bad).length;
  $("liveEnrollHint").innerHTML =
    `每人錄約 ${ENROLL_SECONDS} 秒，請每個人唸<b>同一段話</b>`
    + "（例如「大家好，我是○○○，今天由我負責這個項目」），唸滿整段不要留白"
    + "——固定句子涵蓋的發音較齊全，比隨口一句好比對。"
    + "<br>錄樣本請用<b>開會時的同一支麥克風、同樣的距離</b>；用手機貼著嘴錄、"
    + "開會卻用遠處的桌上型麥克風，聲音差距足以讓比對失效。"
    + `<br>目前 ${ready} / ${enrollPeople.length} 位已備妥；`
    + "沒填姓名或沒錄音的人會被略過，那些人在逐字稿中維持講者代號。"
    + (bad ? `　⚠ 有 ${bad} 位的樣本可能不能用，建議按「試聽」確認後重錄。` : "");
}

// 固定錄 ENROLL_SECONDS 秒後自動停止：比「按開始再按停止」少一半操作，
// 也保證每個人的樣本長度一致，聲紋比對的條件才公平
async function recordEnrollment(index) {
  if (enrollBusy || liveRecording) return;
  // 旗標要在第一個 await 之前就立起來：檢查與設定之間隔著 getUserMedia 的話，
  // 連點兩列會同時通過檢查，兩個錄音器一起搶麥克風——正是這個旗標要防的事
  enrollBusy = true;
  // 錄音中把人數鎖住：中途調少人數會把正在錄的這一列從 enrollPeople 抽掉，
  // onstop 就會踩到 undefined，enrollBusy 永遠留在 true——之後整頁都錄不了
  // 任何人，而且完全沒有錯誤訊息
  $("liveEnrollCount").disabled = true;
  const release = () => { $("liveEnrollCount").disabled = false; enrollBusy = false; };
  const state = $(`enrollState${index}`);
  const availErr = micAvailabilityError();
  if (availErr) { showError(availErr); release(); return; }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia(micConstraints());
  } catch (e) {
    showError("無法使用麥克風錄製聲音樣本：" + micPermissionMessage(e));
    release();
    return;
  }
  populateMicDevices(false);  // 授權後裝置名稱才看得到
  const chunks = [];
  const mime = pickMime();
  const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  let tick = 0;
  rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  rec.onerror = () => {
    showError("錄製聲音樣本時發生錯誤，請再試一次。");
    if (rec.state !== "inactive") rec.stop();  // 觸發 onstop 收尾，別卡在錄音中
  };
  rec.onstop = async () => {
    // 提早停止（錯誤、裝置被拔掉）時倒數還在跑。不清掉的話它會一直寫進已經
    // 被重繪掉的節點，而且每重錄一次就多疊一個計時器
    clearInterval(tick);
    stopMeter();
    stream.getTracks().forEach(t => t.stop());  // 立刻還回麥克風，別佔著
    await acceptSample(index, new Blob(chunks, { type: rec.mimeType }));
    release();
    renderEnrollRows();
  };
  rec.start();
  startMeter(stream, false);  // 串流由 onstop 統一關，音量表不要重複關
  let left = ENROLL_SECONDS;
  state.textContent = `錄音中 ${left}`;
  tick = setInterval(() => {
    left -= 1;
    if (left > 0) { state.textContent = `錄音中 ${left}`; return; }
    clearInterval(tick);
    if (rec.state !== "inactive") rec.stop();
  }, 1000);
}

// 樣本的副檔名。後端用它決定送進比對的音訊型別，標錯會讓那個人比不出來。
// 上傳的檔案優先沿用原檔名的副檔名（.ogg / .flac / .m4a 都可能），
// 自己錄的則依 MediaRecorder 實際產生的 mime 判斷
function sampleExt(blob) {
  const fromName = (blob.name || "").match(/\.[a-z0-9]{2,4}$/i);
  if (fromName) return fromName[0].toLowerCase();
  const type = blob.type || "";
  if (type.includes("mp4") || type.includes("m4a") || type.includes("aac")) return ".mp4";
  if (type.includes("mpeg")) return ".mp3";
  if (type.includes("wav")) return ".wav";
  if (type.includes("ogg")) return ".ogg";
  return ".webm";
}

// 回傳實際上傳成功的人數。任何失敗都只降級成「維持講者代號」，不丟例外出去。
// 併行上傳：四個人在手機網路下逐一上傳要十幾秒，而那段時間會議已經在進行
async function uploadEnrollments(sessionId) {
  if (!enrollOn()) return 0;
  const ready = enrollPeople.filter(p => p.name.trim() && p.blob);
  const names = ready.map(p => p.name.trim());
  if (new Set(names).size !== names.length) {
    showNotice("有兩位以上填了相同的姓名，請改成不同的名字後重新開始，這場先維持講者代號。");
    return 0;
  }
  if (!ready.length) {
    showNotice("預錄聲音辨識人已勾選，但沒有任何一位同時填了姓名並錄好音，這場會維持講者代號。");
    return 0;
  }
  // 最後一次提醒：不能用的樣本比對不出東西，那個人等於白錄
  const bad = ready.filter(p => p.bad).map(p => `${p.name.trim()}（${p.bad}）`);
  if (bad.length) {
    showNotice(
      `「${bad.join("」「")}」的樣本可能不能用，很可能比對不出來（那些人會維持講者代號）。`
      + "如果不是故意的，建議先按「結束會議」重錄再開始。"
    );
  }
  const results = await Promise.all(ready.map(async p => {
    const form = new FormData();
    form.append("file", p.blob, "enroll" + sampleExt(p.blob));
    form.append("name", p.name.trim());
    try {
      await api.liveEnroll(sessionId, form);
      return 1;
    } catch (e) {
      showNotice(`「${p.name.trim()}」的聲音樣本上傳失敗（${e.message}），這個人會維持講者代號。`);
      return 0;
    }
  }));
  return results.reduce((a, b) => a + b, 0);
}

$("liveEnrollOn").addEventListener("change", () => {
  $("liveEnrollBox").style.display = enrollOn() ? "" : "none";
  if (enrollOn()) { renderEnrollRows(); populateMicDevices(false); }
});
$("liveEnrollCount").addEventListener("change", renderEnrollRows);

$("btnLiveStart").addEventListener("click", async () => {
  clearError();
  // 樣本還在錄的時候開始聆聽，那個人會來不及上傳而被略過，但錄完後畫面仍會
  // 顯示「已錄好」——使用者不會發現少了一個人
  if (enrollBusy) {
    showError("聲音樣本還在錄製中，請等這一段錄完再開始聆聽。");
    return;
  }
  const availErr = micAvailabilityError();
  if (availErr) { showError(availErr); return; }
  try {
    liveStream = await buildLiveStream(wantSystemAudio());
  } catch (e) { releaseLiveStreams(); showError(e.message); return; }
  try {
    liveSessionId = (await api.liveStart({
      translate_to: $("liveTranslate").value || null,
      terms: meetingTerms(),  // 會前打的詞彙要進每段轉錄，不是只進最後的分析
    })).session_id;
  } catch (e) { releaseLiveStreams(); showError(e.message); return; }

  // 樣本改成背景併行上傳，不 await：四個人在手機網路下要十幾秒，而開場
  // 正好是自我介紹、最有身分線索的一段——那幾秒不能拿來等上傳。
  // 後端只要求樣本趕在「結束」之前送達，所以結束分析前再等它（見 btnLiveStop）
  enrollUploads = enrollOn()
    ? uploadEnrollments(liveSessionId).catch(() => 0)
    : null;

  liveRecording = true;
  liveStartTime = Date.now();
  liveRecorders = [];  // 上一場若有錄音器沒收乾淨，會擋掉這一場的第一段
  liveSegIndex = 0;
  liveSentCount = 0;
  pendingChunks = [];
  droppedChunks = 0;
  liveSpeakers = {};
  liveTranscriptText = "";
  announcedNames.clear();  // 新的一場重新回報，別沿用上一場認出的人
  acquireWakeLock();  // 保持螢幕常亮，避免手機鎖屏中斷錄音
  $("btnLiveStart").disabled = true;
  $("btnLiveStop").disabled = false;
  $("btnLiveRetry").style.display = "none";  // 開新一場，清掉上一場的重試入口
  $("liveTranscript").style.display = "block";
  $("liveTranscript").innerHTML =
    `<div class="cap-line" id="liveCaret"><span class="cap-time">[--:--]</span><span class="caret-block"></span></div>`;
  liveTickTimer = setInterval(liveTick, 1000);
  liveTick();
  recordSegment();
  openStage();
});

$("btnLiveStop").addEventListener("click", async () => {
  clearError();
  liveRecording = false;
  clearTimeout(liveSegTimer);
  clearInterval(liveTickTimer);
  releaseWakeLock();
  closeStage();
  releaseStageAudio();
  $("btnLiveStage").style.display = "none";
  $("btnLiveStop").disabled = true;
  $("liveStatus").textContent = "整理最後一段錄音…";

  // 重疊期間同時有兩個錄音器，兩個都要停：漏停的那個會在 session 關掉之後
  // 才觸發上傳（400），最後那幾秒就永久消失了
  liveRecorders.slice().forEach(r => { if (r.state !== "inactive") r.stop(); });
  releaseLiveStreams();  // 關掉麥克風、系統音源與混音器

  // 等所有音訊段上傳完成（含最後一段），最多等 3 分鐘
  const deadline = Date.now() + 180000;
  await new Promise(r => setTimeout(r, 300));  // 讓 onstop 先執行
  while (uploadsInFlight > 0 && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 300));
  }

  // 等待期間網路多半已恢復，這時補送稍早失敗的段成功率最高
  const lost = await flushPendingChunks();
  if (lost) {
    showNotice(`有 ${lost} 段錄音始終上傳失敗，逐字稿會缺少那幾段，分析結果請以逐字稿為準核對。`);
  }

  const caret = $("liveCaret");
  if (caret) caret.remove();  // 收起打字游標

  // 樣本是在背景上傳的。沒送達就 finish 的話，後端沒有樣本可比，等於整個
  // 預錄功能白做——所以這裡一定要等它（失敗也只是少幾個名字，不擋分析）
  if (enrollUploads) {
    $("liveStatus").textContent = "整理聲音樣本…";
    try { await enrollUploads; } catch (e) { /* 失敗已在上傳時提示過 */ }
    enrollUploads = null;
  }

  await finishLiveSession();
});

// 預錄了幾位、實際認出幾位。沒有這份回報，畫面上只是「有些人有名字、有些
// 人沒有」，使用者無從判斷是自己樣本錄壞了，還是這個功能根本沒生效
function reportEnrollOutcome(result) {
  const matched = Object.values(result.speakers_matched || {});
  const missed = result.speakers_unmatched || [];
  if (!matched.length && !missed.length) return;  // 這場沒有預錄任何人
  const parts = [];
  if (matched.length) parts.push(`認出 ${matched.join("、")}`);
  if (missed.length) {
    parts.push(
      `沒認出 ${missed.join("、")}（在逐字稿中維持講者代號）——`
      + "多半是樣本太小聲、與開會用的麥克風不同，或那個人整場沒講幾句話"
    );
  }
  showNotice("預錄聲音辨識：" + parts.join("；") + "。");
}

// 結束彙整分析：失敗時「不」丟掉 session id，讓使用者可按「重試分析」再試，
// 不會因為一次額度/網路錯誤就白錄整場會議。
async function finishLiveSession() {
  if (!liveSessionId) return;
  $("btnLiveRetry").style.display = "none";
  $("btnLiveRetry").disabled = true;
  $("liveStatus").textContent = "AI 分析整場會議中…";
  markAnalysisStart();
  showResultSkeleton();
  const options = {
    meeting_date: $("meetingDate").value || null,
    kind: $("meetingKind").value,
    features: selectedFeatures(),
    correct_typos: correctTypos(),
    name_speakers: nameSpeakers(),
    terms: meetingTerms(),
  };
  try {
    let result;
    try {
      result = await api.liveFinish(liveSessionId, options);
    } catch (e) {
      // 404＝聆聽 session 不見了。session 只存在伺服器記憶體，行程一重啟（雲端
      // 重新部署、當掉重生）就永遠找不回來，再按幾次「重試分析」都是同樣的 404。
      // 但逐字稿在瀏覽器這邊還有一份，改走純文字分析把它救回來——這條路是「貼上
      // 文字」既有的流程，不需要 session。
      if (e.status !== 404 || !liveTranscriptText.trim()) throw e;
      result = await api.analyze({ text: liveTranscriptText, ...options });
      // 重啟之後送出的錄音段也會一起 404，所以這份逐字稿可能缺了後半段——
      // 寧可講清楚，也不要讓使用者以為分析的是完整的一場會議
      showNotice("聆聽 session 已遺失（伺服器可能重啟過），已改用瀏覽器保留的逐字稿分析。請核對逐字稿結尾是否完整。");
    }
    reportEnrollOutcome(result);
    $("liveStatus").textContent = "完成";
    liveSessionId = null;
    $("btnLiveStart").disabled = false;
    renderResult(result, result.transcript);
  } catch (e) {
    // 404 走到這裡代表上面的退路也救不了：session 沒了、瀏覽器這份逐字稿又是空的。
    // 這種情況重試永遠是同一個 404，不該再擺一顆按不出結果的按鈕給使用者按
    const unrecoverable = e.status === 404;
    hideResultSkeleton();  // 退回「新會議」，重試分析的按鈕也在那裡
    showError("分析失敗：" + e.message + (unrecoverable
      ? "（這場聆聽沒有留下任何逐字稿，無法分析）"
      : "（逐字稿仍在，可按「重試分析」再試一次）"));
    $("liveStatus").textContent = "分析失敗";
    $("btnLiveStart").disabled = false;  // 也可放棄、重新開始新的一場
    if (!unrecoverable) {
      $("btnLiveRetry").style.display = "inline-flex";
      $("btnLiveRetry").disabled = false;
    }
  }
}
$("btnLiveRetry").addEventListener("click", finishLiveSession);

export { JOB_STATUS_ZH, acquireSystemAudio, acquireWakeLock, appendCaption, buildLiveStream, closeStage, finishLiveSession, initStageAudio, liveMicStream, liveRecording, liveSegIndex, liveSpeakers, liveStream, liveTick, liveTranscriptText, openStage, orb, pickMime, pumpLevel, recordSegment, releaseLiveStreams, releaseStageAudio, releaseWakeLock, transcriptHome, uploadLiveChunk };
