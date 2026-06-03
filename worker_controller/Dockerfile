FROM docker:27-cli AS docker-cli

FROM python:3.11-slim

WORKDIR /app

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

RUN pip install --no-cache-dir httpx==0.27.0

COPY shared /app/shared
COPY worker_controller/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
