/* ==================================================================
   API 層：全站唯一知道端點網址與請求形狀的地方
   ------------------------------------------------------------------
   原本 37 處 fetch("/api/...") 散在 9 支模組裡。集中之後：

   - 之後要做成 app（React Native / Flutter / 原生），網路層是唯一
     需要重寫的檔案，其餘邏輯可以整批搬過去
   - 端點改名或加參數只改一個地方，不必 grep 全專案
   - 認證方式要換（目前是 core.js 的 fetch 攔截塞 Bearer token，
     之後接 Firebase Auth 會換掉）時，改動範圍收斂在這裡

   每支函式回傳已解析的 JSON；HTTP 非 2xx 會 throw 帶 status 的 Error
   （見 core.js 的 jsonOrThrow），呼叫端靠 status 分辨「資料不存在」
   與「暫時性故障」。
   ================================================================== */
import { jsonOrThrow } from "./core.js";

const q = encodeURIComponent;

async function req(url, method = "GET", body) {
  const init = { method };
  if (body instanceof FormData) {
    init.body = body;  // multipart 的 Content-Type 交給瀏覽器自己帶 boundary
  } else if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  return jsonOrThrow(await fetch(url, init));
}

const get = url => req(url);
const post = (url, body) => req(url, "POST", body);
const put = (url, body) => req(url, "PUT", body);
const patch = (url, body) => req(url, "PATCH", body);
const remove = url => req(url, "DELETE");

const api = {
  // ---- 會議 ----
  analyze: payload => post("/api/meetings", payload),
  listMeetings: () => get("/api/meetings"),
  getMeeting: id => get(`/api/meetings/${q(id)}`),
  updateMeeting: (id, fields) => patch(`/api/meetings/${q(id)}`, fields),
  deleteMeeting: id => remove(`/api/meetings/${q(id)}`),
  reanalyze: (id, options) => post(`/api/meetings/${q(id)}/reanalyze`, options),
  replaceTerm: (id, payload) => post(`/api/meetings/${q(id)}/replace-term`, payload),
  meetingKinds: () => get("/api/meeting-kinds"),

  // ---- 任務 ----
  listTasks: () => get("/api/tasks"),
  createTask: task => post("/api/tasks", task),
  updateTask: (id, fields) => patch(`/api/tasks/${q(id)}`, fields),
  deleteTask: id => remove(`/api/tasks/${q(id)}`),

  // ---- 提醒 / 用量 / 健康檢查 ----
  reminders: () => get("/api/reminders"),
  usage: () => get("/api/usage"),
  health: () => get("/api/health"),

  // ---- 詞彙表 / 講者名冊 ----
  glossary: () => get("/api/glossary"),
  saveGlossary: payload => put("/api/glossary", payload),          // { terms }
  speakers: () => get("/api/speakers"),
  saveSpeakers: payload => put("/api/speakers", payload),          // { names }
  rememberSpeakers: payload => post("/api/speakers", payload),     // { names }

  // ---- 問答 / 搜尋 / 翻譯 ----
  ask: payload => post("/api/ask", payload),
  search: keyword => get(`/api/search?q=${q(keyword)}`),
  translate: payload => post("/api/translate", payload),           // { text, target }

  // ---- 檔案上傳（背景轉錄工作）----
  uploadMedia: form => post("/api/media", form),
  mediaJob: jobId => get(`/api/media/${q(jobId)}`),

  // ---- 即時聆聽 ----
  liveStart: payload => post("/api/live/start", payload),
  liveChunk: (sessionId, form) => post(`/api/live/${q(sessionId)}/chunk`, form),
  liveFinish: (sessionId, options) => post(`/api/live/${q(sessionId)}/finish`, options),

  // ---- 備份還原 ----
  restore: data => post("/api/restore", data),
};

export { api };
