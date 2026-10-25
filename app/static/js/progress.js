/* ==================================================================
   轉錄進度的剩餘時間推算
   ------------------------------------------------------------------
   上傳一小時的影片要等好幾分鐘，只有百分比時完全不知道還要多久；長段落
   轉錄又常卡在同一個數字不動，看起來像當掉了。

   估算刻意只看「最近這一段」的速率，不看整場平均：第一段最慢（模型暖機、
   檔案要先上傳），用整場平均會一路高估到最後。進度停住時沿用上一次的估算，
   不把數字清掉——清掉只會讓人更焦慮。

   純函式、不碰 DOM：tests/test_progress_eta_js.py 直接用 node 載入測試。
   ================================================================== */

// 推算用的時間窗：只拿最近這幾秒內的樣本算速率
const WINDOW_SECONDS = 45;
// 至少要走過這麼多進度才開口估——開頭就報數字只會是亂猜，
// 而第一個數字錯得最離譜、使用者印象也最深
const MIN_PROGRESS = 0.05;

function createEta() {
  const samples = [];       // [{ t, p }]，t＝經過秒數，p＝0~1
  let last = null;          // 上一次算出來的剩餘秒數（進度停住時沿用）
  let peak = 0;             // 看過的最大進度：進度回頭多半是重試，不當真

  function update(elapsed, progress) {
    const p = Math.max(peak, Math.min(1, Number(progress) || 0));
    peak = p;
    if (p >= 1) {
      last = 0;
      return 0;
    }
    samples.push({ t: elapsed, p });
    while (samples.length > 2 && elapsed - samples[0].t > WINDOW_SECONDS) samples.shift();

    const first = samples[0];
    const dp = p - first.p;
    const dt = elapsed - first.t;
    if (p < MIN_PROGRESS || dp <= 0 || dt <= 0) return last;  // 停住就沿用上一次
    last = Math.max(0, Math.round(((1 - p) * dt) / dp));
    return last;
  }

  return { update };
}

// 秒數 → 「約 3 分 20 秒」。估算本來就不準，超過一分鐘就不報到個位數秒
function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.max(0, Math.round(seconds));
  if (s < 10) return "快好了";
  if (s < 60) return `約還要 ${Math.round(s / 5) * 5} 秒`;
  const m = Math.round(s / 60);
  return `約還要 ${m} 分鐘`;
}

export { createEta, formatEta };
