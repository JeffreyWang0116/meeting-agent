/* ==================================================================
   把會議紀錄存成 Google 文件
   ------------------------------------------------------------------
   做法是「上傳 .docx，請雲端硬碟幫忙轉成 Google 文件」，不是呼叫 Docs API
   逐段組出文件。Docs API 要一條一條下 insertText／updateParagraphStyle 的
   批次指令，而 .docx 我們本來就要產（見 app/docexport.py）——同一份輸出
   同時服務「下載 Word」與「存成 Google 文件」，排版也只需要維護一套。

   權限只要 drive.file：那個範圍只看得到這個 App 自己建立的檔案，拿不到
   使用者既有的任何東西。
   ================================================================== */
import { DRIVE_SCOPE, googleSignedIn, requestGoogleToken } from "./auth.js";
import { $, nativeFetch, showError, showNotice } from "./core.js";

const UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
  + "?uploadType=multipart&fields=id,webViewLink";
const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const BOUNDARY = "meeting-agent-boundary-7f3a9c";

let token = null;  // 只放在記憶體：這是憑證，不進 localStorage

// Drive 的 multipart 上傳要的是 multipart/related，FormData 送出來的是
// multipart/form-data，它不收——所以這個 body 只能自己拼
function relatedBody(metadata, blob) {
  return new Blob([
    `--${BOUNDARY}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n`,
    JSON.stringify(metadata),
    `\r\n--${BOUNDARY}\r\nContent-Type: ${DOCX_MIME}\r\n\r\n`,
    blob,
    `\r\n--${BOUNDARY}--`,
  ]);
}

async function upload(title, blob) {
  const metadata = {
    name: title || "會議紀錄",
    // 指定成 Google 文件的 mimeType，雲端硬碟就會在收檔時幫忙轉檔
    mimeType: "application/vnd.google-apps.document",
  };
  const resp = await nativeFetch(UPLOAD, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": `multipart/related; boundary=${BOUNDARY}`,
    },
    body: relatedBody(metadata, blob),
  });
  if (!resp.ok) {
    const detail = await resp.text();
    // 最常見的是專案沒開 Drive API 或同意畫面沒加這個範圍，兩者都只會回
    // 403 加一段英文。把狀態碼講出來，至少查得下去
    throw new Error(resp.status === 403
      ? "Google 拒絕了（403）。請確認 Google Cloud 專案已啟用 Drive API，且同意畫面包含 drive.file 範圍。"
      : `上傳失敗（${resp.status}）：${detail.slice(0, 120)}`);
  }
  return resp.json();
}

async function exportToGoogleDocs(meetingId, title) {
  if (!googleSignedIn()) {
    showError("要用 Google 帳號登入才能存進雲端硬碟（訪客沒有 Google 帳號）");
    return;
  }
  showNotice("正在建立 Google 文件…");
  try {
    // 先拿檔案再要權限的話，使用者會先等一段下載、才跳出授權視窗，看起來像當掉
    token = token || await requestGoogleToken(DRIVE_SCOPE);
    if (!token) { showError("沒有取得 Google 授權"); return; }
    const resp = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/report.docx`);
    if (!resp.ok) throw new Error(`取得會議紀錄失敗（${resp.status}）`);
    const file = await upload(title, await resp.blob());
    const url = file.webViewLink || `https://docs.google.com/document/d/${file.id}/edit`;
    // 新分頁而不是直接跳走：使用者還在歷史會議頁上做事
    window.open(url, "_blank", "noopener");
    showNotice("已建立 Google 文件，已在新分頁開啟");
  } catch (e) {
    token = null;  // 權杖可能過期了，下次重新要一次
    showError(`存成 Google 文件失敗：${e.message}`);
  }
}

$("meetingRows").addEventListener("click", e => {
  const btn = e.target.closest(".to-gdocs");
  if (btn) exportToGoogleDocs(btn.dataset.id, btn.dataset.title);
});

export { exportToGoogleDocs, relatedBody };
