FROM python:3.11-slim

WORKDIR /app

COPY web/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY web/main.py /app/main.py
COPY web/templates /app/templates

ENV PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
