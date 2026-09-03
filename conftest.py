# 讓 pytest 能從專案根目錄 import app 套件

import os

# 測試一律跑在「沒有認證設定」的狀態下。
#
# app/main.py 底部的 app = create_app() 在「匯入時」就會執行，而它會讀開發者
# 本機的 .env——只要那份 .env 開了 Firebase 登入，整個測試套件就會跟著初始化
# 一個真實的 firebase app，測試結果開始隨本機設定而變。實際踩過：金鑰放進
# .env 之後，firebase 初始化的那組測試因為「app 已存在」而整組變成空轉，
# 單獨跑會過、全部跑會掛。
#
# 認證行為由各測試自己注入 Settings 來驗，不該靠環境變數。這裡設成空字串而
# 不是刪掉：load_dotenv 不會覆寫已存在的鍵，設空才擋得住 .env 又填回來。
for _var in (
    "FIREBASE_WEB_API_KEY",
    "FIREBASE_AUTH_DOMAIN",
    "FIREBASE_PROJECT_ID",
    "FIREBASE_CREDENTIALS_JSON",
    "FIREBASE_CREDENTIALS_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "API_TOKEN",
):
    os.environ[_var] = ""
