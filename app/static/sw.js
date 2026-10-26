// Service worker：滿足 PWA 可安裝條件、代發通知（見 js/notify.js），並接住推播。
// 刻意不做快取——本 App 的內容都是即時 API 資料，離線快取只會造成過期畫面。
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

// 伺服器推播（見 app/push.py）。這是唯一能在 App 關著、手機鎖著時跳出通知的
// 路徑——js/notify.js 那條要頁面正開著，才有人去呼叫它。
self.addEventListener("push", event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    // 內容壞掉時照樣跳一則：靜靜吞掉只會讓使用者覺得通知時好時壞
    data = {};
  }
  event.waitUntil(self.registration.showNotification(data.title || "會議助手", {
    body: data.body || "有新的提醒，點開看看吧。",
    icon: "/static/icon.svg",
    badge: "/static/icon.svg",
    // 與頁面端的每日提醒共用 tag：兩邊都送到時只會留下一則，不會疊成兩則一樣的
    tag: "daily-reminder",
    renotify: true,
    data: { url: data.url || "/#reminder" },
  }));
});

// 點通知：已經開著就切過去，沒開就開到通知指定的頁面
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find(c => new URL(c.url).origin === self.location.origin);
    if (open) return open.focus();
    return self.clients.openWindow(url);
  })());
});
