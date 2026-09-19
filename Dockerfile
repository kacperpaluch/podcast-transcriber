FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    faster-whisper==1.2.1 fastapi==0.115.6 uvicorn==0.34.0 httpx==0.28.1

WORKDIR /app
COPY app.py .

ENV PYTHONUNBUFFERED=1 HF_HUB_DISABLE_XET=1
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
