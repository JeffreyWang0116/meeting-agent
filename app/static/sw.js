// Service worker：滿足 PWA 可安裝條件，並接住推播。
// 刻意不做快取——本 App 的內容都是即時 API 資料，離線快取只會造成過期畫面。
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

// 推播只能在 service worker 裡顯示。iOS 不支援頁面端的 new Notification()，
// 而且「App 關著也要跳」本來就只有 service worker 做得到。
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
    // 固定 tag：每天一則，新的蓋掉舊的，不要在通知中心疊成一整排
    tag: "meeting-agent-reminder",
    renotify: true,
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(windows => {
      // 已經開著就切過去。每點一次就開一個新分頁的話，桌面很快會躺著十個一樣的頁面
      for (const w of windows) {
        if ("focus" in w) return w.focus();
      }
      return self.clients.openWindow(url);
    })
  );
});
