# 線上會議（Discord 等）收不到對方聲音：分享畫面擷取改良與裝置防護

日期：2026-10-10

## 問題
使用者用 Discord 開會時，即時聆聽錄不到對方的聲音。推測的常見原因：
1. Chrome 分享「整個螢幕＋系統音訊」只錄 Windows **預設播放裝置**；Discord 輸出到別的裝置時錄到空白
2. 分享來源選了「視窗」（沒有聲音）或會議助手自己的分頁
3. macOS 的 Chrome 分享整個螢幕常常沒有系統聲音
4. 有音軌但全程靜音時沒有任何提醒

## 決策紀錄（使用者 2026-10-10 核准，三部分都做）
1. 分享畫面擷取：明確要求系統音訊、排除自己分頁、對方聲音不做回音消除／降噪；
   勾選時依作業系統顯示操作說明；接上後監測系統音源，約 20 秒都沒聲音就提醒（附 Discord 修正方法）
2. 類似 OBS 的直接擷取：偵測不到立體聲混音／VB-CABLE 時，說明怎麼開或安裝（mac：BlackHole）
3. 裝置插拔：監聽 devicechange 即時更新清單並提示新裝置；記住的麥克風不在就退回系統預設並告知

## 任務（TDD）
1. 純函式模組 `app/static/js/audioguide.js`＋`tests/test_audio_guide_js.py`（node）：
   `detectPlatform`、`systemAudioGuide`、`loopbackSetupHint`、`silenceWarning`、`addedDevices`、
   `shouldFallbackToDefaultMic`、`createSilenceWatch`
2. `setup.js`：勾選系統音源時顯示說明；沒偵測到回放裝置時顯示設定說明；devicechange 更新清單並提示
3. `inputs.js`：getDisplayMedia 選項；記住的麥克風失效退回預設；系統音源靜音監測
4. README、全套測試、瀏覽器確認畫面文字（分享對話框與 Discord 需真人實測）
