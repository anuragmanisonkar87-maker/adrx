FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: audio/video; tesseract (+ Hindi): text in posters, scanned PDFs and video frames
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static
# data/: the accounts file when DATABASE_URL is not set (deploy/docker-compose.yml keeps it in a volume)
RUN mkdir -p /app/data && chown -R app:app /app
USER app

# Optional: bake the Whisper model into the image so the first audio/video check is not slow.
#   docker build --build-arg PRELOAD_WHISPER=1 [--build-arg WHISPER_MODEL=large-v3-turbo] -t adrx .
ARG WHISPER_MODEL=small
ENV WHISPER_MODEL=${WHISPER_MODEL}
ARG PRELOAD_WHISPER=0
RUN if [ "$PRELOAD_WHISPER" = "1" ]; then \
      python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"; \
    fi

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.getenv('PORT', '8000'), timeout=4)"
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
