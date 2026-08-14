import sys
import os
import uuid
import json
import shutil
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
sys.path.insert(0, "/app/shared")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import httpx
import db

app = FastAPI(title="Podcast Transcriber")
templates = Jinja2Templates(directory="/app/templates")


def normalize_published_at(value: Optional[str]) -> Optional[str]:
    """Store RSS and manual dates in one sortable UTC representation."""
    if not value or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_existing_episode_dates() -> None:
    """Migrate legacy RSS date strings so SQL can sort them chronologically."""
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, published_at FROM episodes WHERE published_at IS NOT NULL"
        ).fetchall()
        for row in rows:
            normalized = normalize_published_at(row["published_at"])
            if normalized and normalized != row["published_at"]:
                conn.execute(
                    "UPDATE episodes SET published_at=? WHERE id=?",
                    (normalized, row["id"]),
                )


os.makedirs("/data/audio", exist_ok=True)
os.makedirs("/data/models", exist_ok=True)
db.init_db()
normalize_existing_episode_dates()


# ── Models ────────────────────────────────────────────────────────────────────

class TranscribeRequest(BaseModel):
    audio_url: str
    language: Optional[str] = None
    episode_title: Optional[str] = None
    feed_name: Optional[str] = None
    rss_feed_title: Optional[str] = None
    feed_url: Optional[str] = None
    guid: Optional[str] = None
    published_at: Optional[str] = None
    duration_seconds: Optional[int] = None


def _queue_request(req: TranscribeRequest, processing_mode: str):
    if not req.audio_url or not req.audio_url.strip():
        raise HTTPException(400, "audio_url is required")
    guid = req.guid or str(uuid.uuid4())
    with db.db() as conn:
        try:
            conn.execute(
                """INSERT INTO episodes(feed_id, guid, rss_title, audio_url, published_at,
                       duration_seconds, language, feed_name, feed_url, rss_feed_title,
                       processing_mode)
                   VALUES(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (guid, req.episode_title, req.audio_url.strip(), normalize_published_at(req.published_at),
                 req.duration_seconds, req.language, req.feed_name, req.feed_url,
                 req.rss_feed_title, processing_mode),
            )
            job_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"Episode with guid '{guid}' already exists")
            raise HTTPException(500, str(e))
    return {"job_id": job_id, "processing_mode": processing_mode}


class SettingsUpdate(BaseModel):
    whisper_model: Optional[str] = None
    webhook_url: Optional[str] = None


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Transcribe API ────────────────────────────────────────────────────────────

@app.post("/api/transcribe", status_code=202)
def queue_transcription(req: TranscribeRequest):
    return _queue_request(req, "transcribe")


@app.post("/api/prepare", status_code=202)
def queue_preparation(req: TranscribeRequest):
    return _queue_request(req, "prepare")


@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: int):
    with db.db() as conn:
        row = conn.execute(
            """SELECT id, status, processing_mode, rss_title, error, duration_seconds,
                      transcribed_seconds, prepared_manifest
               FROM episodes WHERE id=?""",
            (job_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    d = dict(row)
    dur = d.get("duration_seconds") or 0
    done = d.get("transcribed_seconds") or 0
    d["progress_pct"] = min(99, int(done / dur * 100)) if dur > 0 else None
    if d.get("prepared_manifest"):
        d["chunks"] = json.loads(d.pop("prepared_manifest"))
    else:
        d.pop("prepared_manifest", None)
    return d


@app.get("/api/jobs/{job_id}/chunks/{chunk_index}")
def get_prepared_chunk(job_id: int, chunk_index: int):
    """Serve one prepared chunk to n8n without exposing host filesystem paths."""
    with db.db() as conn:
        row = conn.execute(
            "SELECT processing_mode, prepared_manifest FROM episodes WHERE id=?", (job_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    if row["processing_mode"] != "prepare" or not row["prepared_manifest"]:
        raise HTTPException(409, "Audio is not prepared for external transcription")
    try:
        manifest = json.loads(row["prepared_manifest"])
        chunk = next(item for item in manifest if item["index"] == chunk_index)
    except (json.JSONDecodeError, StopIteration, KeyError):
        raise HTTPException(404, "Chunk not found")
    base_dir = os.path.realpath(f"/data/audio/episode_{job_id}_external_stt")
    path = os.path.realpath(os.path.join(base_dir, chunk["file_name"]))
    if os.path.dirname(path) != base_dir or not os.path.isfile(path):
        raise HTTPException(404, "Prepared chunk file is unavailable")
    return FileResponse(path, media_type="audio/mpeg", filename=chunk["file_name"])


@app.post("/api/jobs/{job_id}/cleanup")
def cleanup_prepared_job(job_id: int):
    """Remove prepared audio after n8n completed transcription and summary."""
    with db.db() as conn:
        row = conn.execute("SELECT processing_mode FROM episodes WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Job not found")
        if row["processing_mode"] != "prepare":
            raise HTTPException(409, "Cleanup is only available for prepared jobs")
        shutil.rmtree(f"/data/audio/episode_{job_id}_external_stt", ignore_errors=True)
        conn.execute(
            "UPDATE episodes SET status='done', prepared_manifest=NULL, error=NULL WHERE id=?",
            (job_id,),
        )
    return {"ok": True}


# ── Settings API ──────────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    with db.db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@app.put("/api/settings")
def update_settings(update: SettingsUpdate):
    allowed_models = ["large-v3-turbo", "large-v3", "medium", "small", "parakeet-tdt-0.6b-v3"]
    with db.db() as conn:
        if update.whisper_model is not None:
            if update.whisper_model not in allowed_models:
                raise HTTPException(400, f"Model must be one of: {allowed_models}")
            conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('whisper_model',?)",
                (update.whisper_model,),
            )
        if update.webhook_url is not None:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('webhook_url',?)",
                (update.webhook_url.strip(),),
            )
    return get_settings()


# ── Episodes API ──────────────────────────────────────────────────────────────

@app.get("/api/episodes")
def list_episodes(status: Optional[str] = None, limit: int = 50, offset: int = 0):
    query = """
        SELECT e.id, e.feed_id, COALESCE(f.display_name, e.feed_name) as feed_name, e.rss_title,
               e.status, e.language, e.duration_seconds, e.error,
               e.published_at, e.created_at, e.guid
        FROM episodes e
        LEFT JOIN feeds f ON f.id = e.feed_id
        WHERE 1=1
    """
    params = []
    if status is not None:
        query += " AND e.status=?"
        params.append(status)
    query += " ORDER BY julianday(COALESCE(e.published_at, e.created_at)) DESC, e.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db.db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/episodes/active")
def get_active_episode():
    with db.db() as conn:
        row = conn.execute(
            """SELECT e.id, e.rss_title, COALESCE(f.display_name, e.feed_name) as feed_name,
                      e.duration_seconds, e.transcribed_seconds, e.status
               FROM episodes e LEFT JOIN feeds f ON f.id = e.feed_id
               WHERE e.status = 'transcribing'
               LIMIT 1""",
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    dur = d["duration_seconds"] or 0
    done = d["transcribed_seconds"] or 0
    d["progress_pct"] = min(99, int(done / dur * 100)) if dur > 0 else None
    return d


@app.get("/api/episodes/{episode_id}/transcript")
def get_transcript(episode_id: int):
    with db.db() as conn:
        row = conn.execute(
            "SELECT id, rss_title, transcript, status FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Episode not found")
    return dict(row)


@app.post("/api/episodes/{episode_id}/send-webhook")
def send_webhook_now(episode_id: int):
    with db.db() as conn:
        row = conn.execute(
            """SELECT e.id, e.guid, e.rss_title, e.audio_url, e.published_at,
                      e.transcript, e.language, e.duration_seconds, e.status,
                      COALESCE(f.display_name, e.feed_name) as feed_name,
                      COALESCE(f.url, e.feed_url) as feed_url,
                      COALESCE(f.rss_feed_title, e.rss_feed_title) as rss_feed_title
               FROM episodes e LEFT JOIN feeds f ON f.id = e.feed_id
               WHERE e.id=?""",
            (episode_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Episode not found")
    if not row["transcript"]:
        raise HTTPException(400, "Brak transkrypcji — nie można wysłać webhooka")

    with db.db() as conn:
        setting = conn.execute("SELECT value FROM settings WHERE key='webhook_url'").fetchone()
    webhook_url = setting["value"] if setting else ""
    if not webhook_url:
        raise HTTPException(400, "Brak skonfigurowanego URL webhooka")

    payload = {
        "feed_name": row["feed_name"],
        "rss_feed_title": row["rss_feed_title"],
        "feed_url": row["feed_url"],
        "episode_title": row["rss_title"],
        "guid": row["guid"],
        "audio_url": row["audio_url"],
        "published_at": row["published_at"],
        "language": row["language"],
        "transcript": row["transcript"],
        "duration_seconds": row["duration_seconds"],
    }

    status_code = None
    ok = False
    error_msg = None
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=15)
        status_code = resp.status_code
        ok = resp.status_code < 400
        if not ok:
            error_msg = f"HTTP {resp.status_code}"
    except Exception as e:
        error_msg = str(e)[:200]

    with db.db() as conn:
        conn.execute(
            "INSERT INTO webhook_log(episode_id, status_code, ok, error) VALUES(?,?,?,?)",
            (episode_id, status_code, 1 if ok else 0, error_msg),
        )

    return {"ok": ok, "status_code": status_code, "error": error_msg}


@app.post("/api/episodes/{episode_id}/retry")
def retry_episode(episode_id: int):
    with db.db() as conn:
        row = conn.execute(
            "SELECT id, status FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Episode not found")
        if row["status"] in ("queued", "transcribing"):
            raise HTTPException(400, f"Odcinek już jest w toku (status: {row['status']})")
        conn.execute(
            "UPDATE episodes SET status='queued', error=NULL, transcript=NULL WHERE id=?",
            (episode_id,),
        )
    return {"ok": True}


@app.delete("/api/episodes/finished")
def delete_finished_episodes():
    """Clear finished history without ever interrupting active work."""
    finished_statuses = ("done", "error", "skipped")
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id FROM episodes WHERE status IN (?, ?, ?)", finished_statuses
        ).fetchall()
        episode_ids = [row["id"] for row in rows]
        conn.execute("DELETE FROM episodes WHERE status IN (?, ?, ?)", finished_statuses)

    for episode_id in episode_ids:
        shutil.rmtree(f"/data/audio/episode_{episode_id}_external_stt", ignore_errors=True)
    return {"ok": True, "deleted_count": len(episode_ids)}


@app.delete("/api/episodes/{episode_id}")
def delete_episode(episode_id: int):
    """Remove a non-running episode so its RSS GUID can be processed again."""
    with db.db() as conn:
        row = conn.execute(
            "SELECT status FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Episode not found")
        # `prepared` means the local worker has finished. The user may explicitly
        # discard its pending external-STT job and its chunks before sending the
        # same RSS episode through n8n again.
        if row["status"] in ("queued", "preparing", "transcribing"):
            raise HTTPException(409, "Nie można usunąć odcinka, który jest w toku")
        conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))

    # The directory name is derived solely from the numeric API path parameter.
    shutil.rmtree(f"/data/audio/episode_{episode_id}_external_stt", ignore_errors=True)
    return {"ok": True, "deleted_episode_id": episode_id}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats():
    with db.db() as conn:
        statuses = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM episodes GROUP BY status"
        ).fetchall()
    return {
        "episodes": {r["status"]: r["cnt"] for r in statuses},
    }


# ── Webhook log ──────────────────────────────────────────────────────────────

@app.get("/api/webhook/log")
def get_webhook_log(limit: int = 50):
    with db.db() as conn:
        rows = conn.execute(
            """SELECT wl.id, wl.episode_id, wl.sent_at, wl.status_code, wl.ok, wl.error,
                      e.rss_title, COALESCE(f.display_name, e.feed_name) as feed_name
               FROM webhook_log wl
               JOIN episodes e ON e.id = wl.episode_id
               LEFT JOIN feeds f ON f.id = e.feed_id
               ORDER BY wl.sent_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Test webhook ──────────────────────────────────────────────────────────────

@app.post("/api/webhook/test")
def test_webhook():
    with db.db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='webhook_url'").fetchone()
    webhook_url = row["value"] if row else ""
    if not webhook_url:
        raise HTTPException(400, "Brak skonfigurowanego URL webhooka")

    payload = {
        "feed_name": "Testowy kanał (Podcast Transcriber)",
        "rss_feed_title": "Test RSS Feed",
        "feed_url": "https://example.com/rss.xml",
        "episode_title": "Testowy odcinek — weryfikacja webhooka",
        "guid": "test-webhook-001",
        "audio_url": "https://example.com/test.mp3",
        "published_at": "2026-06-03T12:00:00+00:00",
        "language": "pl",
        "transcript": "To jest testowa transkrypcja wysłana z Podcast Transcriber. Jeśli widzisz tę wiadomość, webhook działa poprawnie.",
        "duration_seconds": 42,
    }

    try:
        resp = httpx.post(webhook_url, json=payload, timeout=15)
        return {
            "ok": resp.status_code < 400,
            "status_code": resp.status_code,
            "response": resp.text[:500] if resp.text else "",
        }
    except httpx.TimeoutException:
        raise HTTPException(504, "Webhook nie odpowiedział w ciągu 15 sekund")
    except Exception as e:
        raise HTTPException(502, f"Błąd połączenia: {e}")
