FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir feedparser==6.0.11 httpx==0.27.0

COPY shared /app/shared
COPY scheduler/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
