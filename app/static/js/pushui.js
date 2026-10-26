/* ==================================================================
   手機推播提醒：設定裡的開關（純函式在 push.js）
   ------------------------------------------------------------------
   取代原本頁面端的 new Notification()：那個 iOS 完全不支援，而且要 App
   正開著才跳得出來——手機上等於沒有提醒。改成 Web Push 之後，訂閱交給
   瀏覽器的推播服務，真正送出的是伺服器端的排程（見 app/push.py）。
   ================================================================== */
import { api } from "./api.js";
import { $, showError } from "./core.js";
import { blockedReason, urlBase64ToUint8Array } from "./push.js";
import { localDailyEnabled, maybeNotifyReminders, setLocalDaily } from "./reminders.js";

const isIOS = () =>
  /iPad|iPhone|iPod/.test(navigator.userAgent)
  // iPadOS 13 起的 Safari 自稱 Macintosh，只有觸控點數看得出它其實是 iPad
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

const isStandalone = () =>
  window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;

const supported = () =>
  "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

// 伺服器公鑰。先抓起來放著：iOS 要求 Notification.requestPermission() 必須在
// 使用者點擊的那一拍裡呼叫，中間插一個 await fetch 會讓提示框根本不出現
let serverKey = null;
let serverEnabled = null;  // null = 還沒問到

async function loadKey() {
  try {
    const cfg = await api.pushKey();
    serverEnabled = !!cfg.enabled;
    serverKey = cfg.public_key || null;
  } catch (e) {
    serverEnabled = false;
  }
}

function setHint(text) {
  const el = $("notifyHint");
  el.textContent = text || "";
  el.hidden = !text;
}

async function registration() {
  if (!("serviceWorker" in navigator)) return null;
  return (await navigator.serviceWorker.getRegistration()) || navigator.serviceWorker.register("/sw.js");
}

async function currentSub() {
  if (!supported()) return null;
  const reg = await navigator.serviceWorker.getRegistration();
  return reg ? reg.pushManager.getSubscription() : null;
}

function payload(sub) {
  return {
    ...sub.toJSON(),
    // 存「比 UTC 快幾分鐘」：getTimezoneOffset() 的正負號與直覺相反，
    // 在這裡轉成正常的方向，後端就不必再記一次這件事
    tz_offset: -new Date().getTimezoneOffset(),
  };
}

async function isOn() {
  return !!(await currentSub()) || localDailyEnabled();
}

async function render() {
  const sub = await currentSub();
  $("btnNotifyToggle").textContent = (sub || localDailyEnabled()) ? "已開啟" : "開啟";
  $("btnNotifyTest").hidden = !sub;
  if (sub) setHint("每天早上 9 點，有逾期或即將到期的待辦時會通知你。App 關著也收得到。");
}

async function askPermission() {
  // 權限要在點擊的同一拍問，前面不能有 await（iOS 會直接忽略）
  const perm = await Notification.requestPermission();
  if (perm === "granted") return true;
  setHint("瀏覽器沒有允許通知。請到這個網站的設定裡把通知打開，再回來開啟一次。");
  return false;
}

// 站台沒設定 VAPID 金鑰時的退路：這個瀏覽器自己跳，但只有 App 開著時才會發生
async function enableLocalOnly() {
  if (!("Notification" in window)) { setHint("這個瀏覽器不支援通知。"); return; }
  if (!await askPermission()) return;
  setLocalDaily(true);
  if (await maybeNotifyReminders(true)) {
    setHint("這個站台沒有設定推播，只能在 App 開著的時候提醒你。");
  } else {
    setLocalDaily(false);
    setHint("這個瀏覽器發不出通知，每日提醒不會出現。");
  }
}

async function enable() {
  if (serverEnabled === false) { await enableLocalOnly(); return; }

  const reason = blockedReason({
    supported: supported(), ios: isIOS(), standalone: isStandalone(), serverEnabled: true,
  });
  if (reason) { setHint(reason); return; }
  if (!await askPermission()) return;

  if (!serverKey) await loadKey();
  if (!serverKey) { await enableLocalOnly(); return; }

  const reg = await registration();
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,  // 不給 silent push，這是 Chrome 與 Safari 的硬性要求
    applicationServerKey: urlBase64ToUint8Array(serverKey),
  });
  await api.pushSubscribe(payload(sub));
  setLocalDaily(false);  // 伺服器會送了，兩邊都送只是同一件事講兩次
  setHint("已開啟。每天早上 9 點，有逾期或即將到期的待辦時會通知你。App 關著也收得到。");
}

async function disable() {
  setLocalDaily(false);
  const sub = await currentSub();
  if (sub) {
    // 先告訴伺服器再退訂：反過來的話退訂成功但伺服器失敗，就會留一筆死訂閱
    await api.pushUnsubscribe({ endpoint: sub.endpoint });
    await sub.unsubscribe();
  }
  setHint("已關閉。");
}

$("btnNotifyToggle").addEventListener("click", async () => {
  const btn = $("btnNotifyToggle");
  btn.disabled = true;
  try {
    if (await isOn()) await disable();
    else await enable();
  } catch (e) {
    showError(`通知設定失敗：${e.message}`);
  } finally {
    btn.disabled = false;
    await render();
  }
});

$("btnNotifyTest").addEventListener("click", async () => {
  const btn = $("btnNotifyTest");
  btn.disabled = true;
  try {
    const out = await api.pushTest();
    setHint(out.sent
      ? "已送出一則，看看手機跳出來了沒。沒跳的話多半是系統設定裡把這個 App 的通知關掉了。"
      : "這個工作區還沒有任何裝置訂閱通知。");
  } catch (e) {
    showError(`試送失敗：${e.message}`);
  } finally {
    btn.disabled = false;
  }
});

// 訂閱記的是「訂閱當下的工作區」。一個瀏覽器只有一組 endpoint，所以切換工作區
// 時要重訂一次，否則人在群組裡卻收到個人工作區的提醒
window.addEventListener("workspacechange", async () => {
  const sub = await currentSub();
  if (sub) await api.pushSubscribe(payload(sub)).catch(() => {});
});

loadKey().then(render);

export { currentSub, isIOS, isOn, isStandalone, render, supported };
