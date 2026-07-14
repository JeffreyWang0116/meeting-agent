// 最小 service worker：滿足 PWA 可安裝條件。
// 刻意不做快取——本 App 的內容都是即時 API 資料，離線快取只會造成過期畫面。
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
