# 雲端部署映像：Python + ffmpeg（把影片/webm/m4a 轉成 wav 再交給 Gemini 轉錄）
FROM python:3.12-slim

# ffmpeg 用來正規化各種音訊/影片格式；Gemini 只吃標準音訊
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先裝依賴，讓 Docker 快取這一層（只用精簡的雲端依賴，不含 faster-whisper）
COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY app ./app

# 雲端沒有 GPU，轉錄後端切成 Gemini
ENV TRANSCRIBE_ENGINE=gemini
ENV PYTHONUNBUFFERED=1

# 平台會用 $PORT 指定對外埠（Render/Railway 皆然），預設 8000 供本地測試
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
