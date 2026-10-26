// 最小 service worker：滿足 PWA 可安裝條件，並代發通知（見 js/notify.js）。
// 刻意不做快取——本 App 的內容都是即時 API 資料，離線快取只會造成過期畫面。
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

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
