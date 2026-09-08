import { api } from "./api.js";
import { $, clearError, esc, showError, showNotice } from "./core.js";
import { hideResultSkeleton, markAnalysisStart, renderResult, showResultSkeleton } from "./result.js";
import { chunkSeconds, correctTypos, meetingTerms, nameSpeakers, selectedFeatures, sysSourceValue, wantSystemAudio } from "./setup.js";
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

    while (true) {
      await new Promise(r => setTimeout(r, 1500));
      const job = await api.mediaJob(job_id);
      const pct = Math.round((job.progress || 0) * 100);
      $("fileProgress").firstElementChild.style.width = pct + "%";
      $("fileStatus").textContent =
        JOB_STATUS_ZH[job.status] + (job.status === "transcribing" ? `（${pct}%）` : "");
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
let liveSegIndex = 0, liveSentCount = 0, liveWakeLock = null, liveStarting = false;

// 取得要錄的串流。withSystemAudio 為真時，額外抓耳機／系統音源（對方的聲音），
// 和麥克風混成一條軌一起錄。音源來源由 sysSourceValue() 決定：
//   deviceId → 直接用 getUserMedia 錄該回放裝置（立體聲混音等），免跳分享視窗；
//   "display" → 用 getDisplayMedia 分享畫面擷取（相容性最高，但每次會跳分享視窗）。
async function buildLiveStream(withSystemAudio) {
  try {
    liveMicStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    throw new Error("無法取得麥克風權限：" + e.message + "（請到瀏覽器設定允許此網站使用麥克風）");
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
  if (liveRecorder && liveRecorder.state === "inactive") recordSegment();  // 錄音若被系統中斷則自動接續
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

async function postLiveChunk(blob, offsetSeconds) {
  const ext = blob.type.includes("mp4") ? ".mp4" : ".webm";
  const form = new FormData();
  form.append("file", blob, "chunk" + ext);
  // 本段在整場會議中的開始秒數：後端把段內相對時間戳平移成整場時間
  if (offsetSeconds != null) form.append("offset", offsetSeconds);
  const r = await api.liveChunk(liveSessionId, form);
  if (r.transcript) liveTranscriptText = r.transcript;
  if (r.text) appendCaption(r.text, r.translation);
}

async function uploadLiveChunk(blob, offsetSeconds) {
  // uploadsInFlight 要涵蓋整個重試過程：結束會議時會等它歸零才送出分析，
  // 不然重試中的那一段就趕不上，等於還是掉了
  uploadsInFlight++;
  liveSentCount++;
  try {
    for (let attempt = 0; ; attempt++) {
      try {
        await postLiveChunk(blob, offsetSeconds);
        return;
      } catch (e) {
        if (attempt >= RETRY_DELAYS_MS.length || !worthRetrying(e)) {
          // 還沒放棄：排進佇列，結束會議前會再試一輪
          if (pendingChunks.length < MAX_PENDING_CHUNKS) {
            pendingChunks.push({ blob, offsetSeconds });
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
    try { await postLiveChunk(c.blob, c.offsetSeconds); }
    catch (e) { stillFailed.push(c); }
  }
  return stillFailed.length + droppedChunks;
}

// 每段用「新的 MediaRecorder」錄，確保每段都有完整檔頭、可獨立解碼。
// liveStarting 旗標＋「已在錄就不重啟」的檢查，避免 onstop 與 visibilitychange
// 同時觸發時建立兩個錄音器造成段落重複。
function recordSegment() {
  if (!liveRecording || liveStarting) return;
  if (liveRecorder && liveRecorder.state === "recording") return;
  liveStarting = true;
  const chunks = [];
  const mime = pickMime();
  const recorder = new MediaRecorder(liveStream, mime ? { mimeType: mime } : undefined);
  liveRecorder = recorder;
  const segStart = Math.floor((Date.now() - liveStartTime) / 1000);  // 本段在整場中的開始秒數
  recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  recorder.onerror = e => showError("錄音發生錯誤：" + ((e.error && e.error.message) || "未知原因"));
  recorder.onstop = () => {
    if (liveRecording) recordSegment();  // 先無縫接錄下一段，再上傳
    const blob = new Blob(chunks, { type: recorder.mimeType });
    if (blob.size > 0) uploadLiveChunk(blob, segStart);
  };
  recorder.start();
  liveStarting = false;
  // 第一段縮短到 12 秒：讓使用者快速看到第一句逐字稿，確認「真的有在聽」
  const secs = liveSegIndex === 0 ? Math.min(12, chunkSeconds) : chunkSeconds;
  liveSegIndex++;
  liveSegTimer = setTimeout(() => {
    if (recorder.state !== "inactive") recorder.stop();
  }, secs * 1000);
}

// ---- 預錄聲音辨識人（選用功能）----
// 會前請每位與會者各錄一小段話，按下「開始聆聽」時連同 session 一起送到後端；
// 結束分析時由聲紋比對把逐字稿的「講者A/B/C」換成真實姓名。
//
// 沒勾這個功能就完全不執行：不開麥克風、不上傳、不多打任何 API，整條路徑與
// 這個功能不存在時相同。錄音失敗或上傳失敗也一律只降級成「維持講者代號」，
// 絕不擋住聆聽本身——會議內容遠比名字重要。
const ENROLL_SECONDS = 10;  // 太短聲紋特徵不足、太長浪費額度；實測 10 秒足夠
let enrollPeople = [];      // 依序對應畫面上每一列：{ name, blob }
let enrollBusy = false;     // 同時只錄一個人，避免兩列搶同一支麥克風

function enrollOn() {
  return $("liveEnrollOn").checked;
}

function syncEnrollPeople() {
  const n = Number($("liveEnrollCount").value || 0);
  while (enrollPeople.length < n) enrollPeople.push({ name: "", blob: null });
  enrollPeople.length = n;  // 人數調少時，多出來的樣本一併丟掉
}

function renderEnrollRows() {
  syncEnrollPeople();
  $("liveEnrollList").innerHTML = enrollPeople.map((p, i) => `
    <div class="enroll-row${p.blob ? " done" : ""}">
      <span class="enroll-no">${i + 1}.</span>
      <input type="text" id="enrollName${i}" maxlength="20"
             placeholder="第 ${i + 1} 位的姓名" value="${esc(p.name)}">
      <button class="ghost" type="button" id="enrollRec${i}">
        <svg class="i-sm" aria-hidden="true"><use href="/static/icons.svg#mic"/></svg>
        ${p.blob ? "重錄" : "錄音"}
      </button>
      <span class="enroll-state" id="enrollState${i}">${p.blob ? "已錄好" : "未錄"}</span>
    </div>`).join("");
  enrollPeople.forEach((p, i) => {
    // 姓名寫回資料模型，重繪（換人數、錄完音）時才不會把使用者打的字弄丟
    $(`enrollName${i}`).addEventListener("input", e => { p.name = e.target.value; });
    $(`enrollRec${i}`).addEventListener("click", () => recordEnrollment(i));
  });
  const ready = enrollPeople.filter(p => p.name.trim() && p.blob).length;
  $("liveEnrollHint").textContent =
    `每人錄約 ${ENROLL_SECONDS} 秒，說一兩句話即可（例如自我介紹）。`
    + `目前 ${ready} / ${enrollPeople.length} 位已備妥；`
    + "沒填姓名或沒錄音的人會被略過，那些人在逐字稿中維持講者代號。";
}

// 固定錄 ENROLL_SECONDS 秒後自動停止：比「按開始再按停止」少一半操作，
// 也保證每個人的樣本長度一致，聲紋比對的條件才公平
async function recordEnrollment(index) {
  if (enrollBusy || liveRecording) return;
  const state = $(`enrollState${index}`);
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    showError("無法使用麥克風錄製聲音樣本：" + e.message);
    return;
  }
  enrollBusy = true;
  // 錄音中把人數鎖住：中途調少人數會把正在錄的這一列從 enrollPeople 抽掉，
  // onstop 就會踩到 undefined，enrollBusy 永遠留在 true——之後整頁都錄不了
  // 任何人，而且完全沒有錯誤訊息
  $("liveEnrollCount").disabled = true;
  const chunks = [];
  const mime = pickMime();
  const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  rec.onerror = () => {
    showError("錄製聲音樣本時發生錯誤，請再試一次。");
    if (rec.state !== "inactive") rec.stop();  // 觸發 onstop 收尾，別卡在錄音中
  };
  rec.onstop = () => {
    stream.getTracks().forEach(t => t.stop());  // 立刻還回麥克風，別佔著
    const blob = new Blob(chunks, { type: rec.mimeType });
    if (blob.size && enrollPeople[index]) enrollPeople[index].blob = blob;
    $("liveEnrollCount").disabled = false;
    enrollBusy = false;
    renderEnrollRows();
  };
  rec.start();
  let left = ENROLL_SECONDS;
  state.textContent = `錄音中 ${left}`;
  const tick = setInterval(() => {
    left -= 1;
    if (left > 0) { state.textContent = `錄音中 ${left}`; return; }
    clearInterval(tick);
    if (rec.state !== "inactive") rec.stop();
  }, 1000);
}

// 回傳實際上傳成功的人數。任何失敗都只降級成「維持講者代號」，不丟例外出去
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
  let done = 0;
  for (const p of ready) {
    const form = new FormData();
    form.append("file", p.blob, "enroll" + (p.blob.type.includes("mp4") ? ".mp4" : ".webm"));
    form.append("name", p.name.trim());
    try {
      await api.liveEnroll(sessionId, form);
      done++;
    } catch (e) {
      showNotice(`「${p.name.trim()}」的聲音樣本上傳失敗（${e.message}），這個人會維持講者代號。`);
    }
  }
  return done;
}

$("liveEnrollOn").addEventListener("change", () => {
  $("liveEnrollBox").style.display = enrollOn() ? "" : "none";
  if (enrollOn()) renderEnrollRows();
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
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showError("此瀏覽器無法使用麥克風：麥克風只在 https:// 加密連線（或 localhost）下可用，請確認網址是 https 開頭");
    return;
  }
  try {
    liveStream = await buildLiveStream(wantSystemAudio());
  } catch (e) { releaseLiveStreams(); showError(e.message); return; }
  try {
    liveSessionId = (await api.liveStart({
      translate_to: $("liveTranslate").value || null,
      terms: meetingTerms(),  // 會前打的詞彙要進每段轉錄，不是只進最後的分析
    })).session_id;
  } catch (e) { releaseLiveStreams(); showError(e.message); return; }

  // 樣本必須趕在第一段音訊之前送達：後端結束時才比對得出誰是誰。
  // 這裡失敗只會少幾個名字，不影響聆聽本身
  if (enrollOn()) $("liveStatus").textContent = "上傳聲音樣本…";
  await uploadEnrollments(liveSessionId);

  liveRecording = true;
  liveStartTime = Date.now();
  liveSegIndex = 0;
  liveSentCount = 0;
  pendingChunks = [];
  droppedChunks = 0;
  liveSpeakers = {};
  liveTranscriptText = "";
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

  if (liveRecorder && liveRecorder.state !== "inactive") liveRecorder.stop();  // 觸發最後一段上傳
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

  await finishLiveSession();
});

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
