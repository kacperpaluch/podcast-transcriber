"""Podcast Transcriber — URL audio wchodzi, transkrypt wychodzi na webhook n8n.

Jeden proces: API + kolejka + model. Model ładuje się przy pierwszym zadaniu
i jest zwalniany po okresie bezczynności, więc aplikacja czekająca na robotę
zajmuje ~100 MB zamiast ~1,5 GB. Transkrypcje są sekwencyjne, bo wątek roboczy
jest jeden — to gwarantuje stałe zużycie RAM niezależnie od długości kolejki.
"""
import gc
import html
import logging
import os
import sqlite3
import threading
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "/data/app.db")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
MODELS_DIR = os.environ.get("MODELS_DIR", "/data/models")
MODEL = os.environ.get("MODEL", "large-v3-turbo")
CPU_THREADS = int(os.environ.get("CPU_THREADS", "4"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
IDLE_UNLOAD_SECS = int(os.environ.get("IDLE_UNLOAD_SECS", "300"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
RETRY_DELAYS = [5, 15, 30, 60, 120]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("podcast")

FIELDS = ("audio_url", "guid", "episode_title", "feed_name", "feed_url",
          "rss_feed_title", "published_at", "language")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(MODELS_DIR, exist_ok=True)
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audio_url TEXT NOT NULL, guid TEXT UNIQUE,
            episode_title TEXT, feed_name TEXT, feed_url TEXT, rss_feed_title TEXT,
            published_at TEXT, language TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            duration_seconds INTEGER, progress_seconds INTEGER DEFAULT 0,
            transcript TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        try:  # migracja dla baz sprzed wprowadzenia licznika prób
            c.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Po restarcie dokończ to, co było w locie — ale zadanie, które ubiło
        # proces MAX_ATTEMPTS razy (np. przez OOM), odkłada się na bok zamiast
        # restartować aplikację w nieskończoność.
        c.execute("UPDATE jobs SET status='error', error='przekroczono limit prób' "
                  "WHERE status='running' AND attempts >= ?", (MAX_ATTEMPTS,))
        c.execute("UPDATE jobs SET status='queued' WHERE status='running'")


def payload_for(row) -> dict:
    """Kontrakt z n8n — kształt ustalony, nie zmieniać bez zmiany workflow."""
    return {
        "event": "transcription_completed",
        "feed_name": row["feed_name"],
        "rss_feed_title": row["rss_feed_title"],
        "feed_url": row["feed_url"],
        "episode_title": row["episode_title"],
        "guid": row["guid"],
        "audio_url": row["audio_url"],
        "published_at": row["published_at"],
        "language": row["language"],
        "duration_seconds": row["duration_seconds"],
        "transcript": row["transcript"],
    }


def send_webhook(row) -> bool:
    if not WEBHOOK_URL:
        log.warning("Brak WEBHOOK_URL — pomijam wysyłkę")
        return True
    for attempt, delay in enumerate([0] + RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            r = httpx.post(WEBHOOK_URL, json=payload_for(row), timeout=30)
            if r.status_code < 400:
                log.info("Webhook OK (%s) dla %s", r.status_code, row["id"])
                return True
            log.warning("Webhook %s, próba %d", r.status_code, attempt + 1)
        except Exception as e:
            log.warning("Webhook błąd (%s), próba %d", e, attempt + 1)
    return False


def transcribe(model, path: str, job_id: int, language):
    segments, info = model.transcribe(
        path, language=language, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500}, beam_size=5)
    with db() as c:
        c.execute("UPDATE jobs SET duration_seconds=?, language=? WHERE id=?",
                  (int(info.duration or 0), info.language, job_id))
    parts, saved_at = [], 0.0
    for seg in segments:
        parts.append(seg.text.strip())
        if seg.end - saved_at >= 30:
            saved_at = seg.end
            with db() as c:
                c.execute("UPDATE jobs SET progress_seconds=? WHERE id=?", (int(seg.end), job_id))
    return "\n".join(parts)


def process(model, job_id: int):
    with db() as c:
        c.execute("UPDATE jobs SET status='running', attempts=attempts+1 WHERE id=?", (job_id,))
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    path = os.path.join(AUDIO_DIR, f"job_{job_id}.audio")
    log.info("Pobieram %s", row["audio_url"])
    with httpx.stream("GET", row["audio_url"], follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)

    try:
        log.info("Transkrybuję zadanie %d", job_id)
        text = transcribe(model, path, job_id, row["language"])
    finally:
        os.remove(path)

    with db() as c:
        c.execute("UPDATE jobs SET transcript=?, status='done' WHERE id=?", (text, job_id))
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    log.info("Gotowe: zadanie %d, %d znaków", job_id, len(text))

    if not send_webhook(row):
        with db() as c:
            c.execute("UPDATE jobs SET error='webhook nieudany' WHERE id=?", (job_id,))


def worker():
    """Model ładowany na żądanie i zwalniany po IDLE_UNLOAD_SECS bezczynności —
    bezczynna aplikacja zajmuje ~100 MB zamiast ~1,5 GB. Ładowanie z cache
    trwa ~3 s, więc przy serii odcinków płaci się za nie raz."""
    model, idle_since = None, time.time()
    while True:
        with db() as c:
            row = c.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            if model is not None and time.time() - idle_since > IDLE_UNLOAD_SECS:
                log.info("Kolejka pusta — zwalniam model")
                model = None
                gc.collect()
            time.sleep(5)
            continue
        if model is None:
            log.info("Ładuję model %s (int8, %d wątki)", MODEL, CPU_THREADS)
            from faster_whisper import WhisperModel
            model = WhisperModel(MODEL, device="cpu", compute_type="int8",
                                 download_root=MODELS_DIR, cpu_threads=CPU_THREADS)
        try:
            process(model, row["id"])
        except Exception as e:
            log.exception("Zadanie %d nieudane", row["id"])
            with db() as c:
                c.execute("UPDATE jobs SET status='error', error=? WHERE id=?",
                          (str(e)[:500], row["id"]))
        idle_since = time.time()


app = FastAPI(title="Podcast Transcriber")


class Job(BaseModel):
    audio_url: str
    guid: str | None = None
    episode_title: str | None = None
    feed_name: str | None = None
    feed_url: str | None = None
    rss_feed_title: str | None = None
    published_at: str | None = None
    language: str | None = None


@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=worker, daemon=True).start()


@app.post("/api/transcribe", status_code=202)
def queue(job: Job):
    if not job.audio_url.strip():
        raise HTTPException(400, "audio_url jest wymagany")
    vals = [getattr(job, f) for f in FIELDS]
    with db() as c:
        try:
            cur = c.execute(
                f"INSERT INTO jobs({','.join(FIELDS)}) VALUES({','.join('?' * len(FIELDS))})",
                vals)
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"guid '{job.guid}' już istnieje")
    return {"job_id": cur.lastrowid}


@app.get("/api/jobs/{job_id}")
def status(job_id: int):
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "nie ma takiego zadania")
    return dict(row)


@app.get("/", response_class=HTMLResponse)
def index():
    with db() as c:
        rows = c.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 50").fetchall()
    trs = ""
    for r in rows:
        pct = f'{100 * (r["progress_seconds"] or 0) // r["duration_seconds"]}%' \
            if r["duration_seconds"] else ""
        trs += (f'<tr><td>{r["id"]}</td><td>{html.escape(r["episode_title"] or "—")}</td>'
                f'<td>{r["status"]}</td><td>{pct}</td>'
                f'<td>{html.escape(r["error"] or "")}</td><td>{r["created_at"]}</td></tr>')
    return (f'<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
            f'<title>Podcast Transcriber</title>'
            f'<style>body{{font:14px system-ui;margin:2rem;max-width:60rem}}'
            f'table{{border-collapse:collapse;width:100%}}'
            f'td,th{{border-bottom:1px solid #ddd;padding:.4rem;text-align:left}}</style>'
            f'<h1>Podcast Transcriber</h1><p>model: {MODEL} · kolejka sekwencyjna</p>'
            f'<table><tr><th>#<th>tytuł<th>status<th>postęp<th>błąd<th>dodano</tr>{trs}</table>')
