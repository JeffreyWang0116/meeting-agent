# 雲端部署映像：Python + ffmpeg（把影片/webm/m4a 轉成 wav 再交給 Gemini 轉錄）
FROM python:3.12-slim

# ffmpeg：正規化各種音訊/影片格式，Gemini 只吃標準音訊
# fonts-noto-cjk：匯出 PDF 要有中文字型可以「內嵌」。少了它，產出的 PDF 版面
#   完全正確但每一個字都是別的字，而且在有系統字型的開發機上看起來完全正常
#   （見 app/docexport.py 的 CJK_FONT_PATHS）
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先裝依賴，讓 Docker 快取這一層。
# 裝的是完整鎖定版本：與 CI 測過的完全一致，重新建置不會突然換到新的大版本
COPY requirements-cloud.lock .
RUN pip install --no-cache-dir -r requirements-cloud.lock

COPY app ./app

ENV PYTHONUNBUFFERED=1

# 平台會用 $PORT 指定對外埠（Render/Railway 皆然），預設 8000 供本地測試
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
