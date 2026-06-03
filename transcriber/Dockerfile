# Build for ARM64 (Raspberry Pi 4/5 64-bit)
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    faster-whisper==1.2.1

COPY shared /app/shared
COPY transcriber/transcribe.py /app/transcribe.py

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "transcribe.py"]
