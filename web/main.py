import sys
import os
import hashlib
sys.path.insert(0, "/app/shared")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import feedparser
import httpx
import db

app = FastAPI(title="Podcast Transcriber")
templates = Jinja2Templates(directory="/app/templates")

os.makedirs("/data/audio", exist_ok=True)
os.makedirs("/data/models", exist_ok=True)
db.init_db()


# ── Models ────────────────────────────────────────────────────────────────────

class FeedCreate(BaseModel):
    display_name: str
    url: str


class FeedUpdate(BaseModel):
    display_name: Optional[str] = None
    enabled: Optional[bool] = None


class SettingsUpdate(BaseModel):
    check_interval_minutes: Optional[int] = None
    whisper_model: Optional[str] = None
    webhook_url: Optional[str] = None


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Feeds API ─────────────────────────────────────────────────────────────────

@app.get("/api/feeds")
def list_feeds():
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, display_name, url, enabled, rss_feed_title, created_at FROM feeds ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/feeds", status_code=201)
def add_feed(feed: FeedCreate):
    if not feed.display_name.strip():
        raise HTTPException(400, "display_name is required")
    if not feed.url.strip():
        raise HTTPException(400, "url is required")
    try:
        with db.db() as conn:
            conn.execute(
                "INSERT INTO feeds(display_name, url) VALUES(?, ?)",
                (feed.display_name.strip(), feed.url.strip()),
            )
            row = conn.execute(
                "SELECT id, display_name, url, enabled, rss_feed_title FROM feeds WHERE url=?",
                (feed.url.strip(),),
            ).fetchone()
        return dict(row)
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, "Feed URL already exists")
        raise HTTPException(500, str(e))


@app.patch("/api/feeds/{feed_id}")
def update_feed(feed_id: int, update: FeedUpdate):
    with db.db() as conn:
        row = conn.execute("SELECT id FROM feeds WHERE id=?", (feed_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Feed not found")
        if update.display_name is not None:
            conn.execute(
                "UPDATE feeds SET display_name=? WHERE id=?",
                (update.display_name.strip(), feed_id),
            )
        if update.enabled is not None:
            conn.execute(
                "UPDATE feeds SET enabled=? WHERE id=?",
                (1 if update.enabled else 0, feed_id),
            )
        row = conn.execute(
            "SELECT id, display_name, url, enabled, rss_feed_title FROM feeds WHERE id=?",
            (feed_id,),
        ).fetchone()
    return dict(row)


@app.delete("/api/feeds/{feed_id}", status_code=204)
def delete_feed(feed_id: int):
    with db.db() as conn:
        row = conn.execute("SELECT id FROM feeds WHERE id=?", (feed_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Feed not found")
        conn.execute("DELETE FROM feeds WHERE id=?", (feed_id,))


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
        if update.check_interval_minutes is not None:
            if update.check_interval_minutes < 1:
                raise HTTPException(400, "Interval must be >= 1 minute")
            conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('check_interval_minutes',?)",
                (str(update.check_interval_minutes),),
            )
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
def list_episodes(feed_id: Optional[int] = None, status: Optional[str] = None, limit: int = 50):
    query = """
        SELECT e.id, e.feed_id, f.display_name as feed_name, e.rss_title,
               e.status, e.language, e.duration_seconds, e.error,
               e.published_at, e.created_at, e.guid
        FROM episodes e
        JOIN feeds f ON f.id = e.feed_id
        WHERE 1=1
    """
    params = []
    if feed_id is not None:
        query += " AND e.feed_id=?"
        params.append(feed_id)
    if status is not None:
        query += " AND e.status=?"
        params.append(status)
    query += " ORDER BY e.created_at DESC LIMIT ?"
    params.append(limit)
    with db.db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/episodes/active")
def get_active_episode():
    """Zwraca aktualnie transkrybowany odcinek z procentem postępu."""
    with db.db() as conn:
        row = conn.execute(
            """SELECT e.id, e.rss_title, f.display_name as feed_name,
                      e.duration_seconds, e.transcribed_seconds, e.status
               FROM episodes e JOIN feeds f ON f.id = e.feed_id
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
                      f.display_name as feed_name, f.url as feed_url, f.rss_feed_title
               FROM episodes e JOIN feeds f ON f.id = e.feed_id
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

    # Zawsze zwracamy 200 — frontend sam ocenia ok/error na podstawie pola ok
    return {"ok": ok, "status_code": status_code, "error": error_msg}


@app.post("/api/episodes/{episode_id}/retry")
def retry_episode(episode_id: int):
    with db.db() as conn:
        row = conn.execute(
            "SELECT id, status FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Episode not found")
        if row["status"] not in ("error",):
            raise HTTPException(400, f"Cannot retry episode with status '{row['status']}'")
        conn.execute(
            "UPDATE episodes SET status='queued', error=NULL, transcript=NULL WHERE id=?",
            (episode_id,),
        )
    return {"ok": True}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats():
    with db.db() as conn:
        total_feeds = conn.execute("SELECT COUNT(*) FROM feeds WHERE enabled=1").fetchone()[0]
        statuses = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM episodes GROUP BY status"
        ).fetchall()
    return {
        "active_feeds": total_feeds,
        "episodes": {r["status"]: r["cnt"] for r in statuses},
    }


# ── Webhook log ──────────────────────────────────────────────────────────────

@app.get("/api/webhook/log")
def get_webhook_log(limit: int = 50):
    with db.db() as conn:
        rows = conn.execute(
            """SELECT wl.id, wl.episode_id, wl.sent_at, wl.status_code, wl.ok, wl.error,
                      e.rss_title, f.display_name as feed_name
               FROM webhook_log wl
               JOIN episodes e ON e.id = wl.episode_id
               JOIN feeds f ON f.id = e.feed_id
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


# ── Fetch latest episode ──────────────────────────────────────────────────────

def _find_audio_url(entry):
    for enc in getattr(entry, "enclosures", []):
        url = getattr(enc, "href", "") or getattr(enc, "url", "")
        ctype = getattr(enc, "type", "")
        if url and ("audio" in ctype or url.lower().split("?")[0].endswith((".mp3", ".m4a", ".ogg", ".opus", ".wav"))):
            return url
    for link in getattr(entry, "links", []):
        if "audio" in link.get("type", ""):
            return link.get("href", "")
    return None


def _parse_date(entry):
    import email.utils
    published = getattr(entry, "published", None)
    if published:
        try:
            t = email.utils.parsedate_to_datetime(published)
            return t.isoformat()
        except Exception:
            return published
    return None


@app.post("/api/feeds/{feed_id}/fetch-latest")
def fetch_latest_episode(feed_id: int):
    with db.db() as conn:
        feed = conn.execute(
            "SELECT id, display_name, url FROM feeds WHERE id=?", (feed_id,)
        ).fetchone()
    if not feed:
        raise HTTPException(404, "Feed not found")

    try:
        parsed = feedparser.parse(feed["url"])
    except Exception as e:
        raise HTTPException(502, f"Nie można pobrać RSS: {e}")

    if parsed.bozo and not parsed.entries:
        raise HTTPException(502, f"Błąd parsowania RSS: {parsed.bozo_exception}")

    # Szukamy pierwszego wpisu z plikiem audio
    entry = None
    audio_url = None
    for e in parsed.entries:
        url = _find_audio_url(e)
        if url:
            entry = e
            audio_url = url
            break

    if not entry or not audio_url:
        raise HTTPException(404, "Nie znaleziono odcinka z plikiem audio w tym feedzie")

    guid = getattr(entry, "id", None) or getattr(entry, "link", None)
    if not guid:
        raw = f"{entry.get('title','')}{entry.get('published','')}"
        guid = hashlib.sha1(raw.encode()).hexdigest()

    rss_title = getattr(entry, "title", None)
    published_at = _parse_date(entry)
    rss_feed_title = getattr(parsed.feed, "title", None)

    # Aktualizuj rss_feed_title w feedzie
    with db.db() as conn:
        conn.execute("UPDATE feeds SET rss_feed_title=? WHERE id=?", (rss_feed_title, feed_id))

    # Sprawdź czy odcinek już istnieje
    with db.db() as conn:
        existing = conn.execute(
            "SELECT id, status FROM episodes WHERE guid=?", (guid,)
        ).fetchone()

    if existing:
        ep_id = existing["id"]
        status = existing["status"]
        if status in ("queued", "transcribing"):
            return {
                "queued": False,
                "episode_id": ep_id,
                "rss_title": rss_title,
                "message": f"Odcinek już jest w kolejce (status: {status})",
            }
        # Już przetworzony lub błąd — reset do queued żeby przetestować ponownie
        with db.db() as conn:
            conn.execute(
                "UPDATE episodes SET status='queued', error=NULL, transcript=NULL, audio_url=? WHERE id=?",
                (audio_url, ep_id),
            )
        return {
            "queued": True,
            "episode_id": ep_id,
            "rss_title": rss_title,
            "message": f"Odcinek ponownie dodany do kolejki (był: {status})",
        }
    else:
        with db.db() as conn:
            conn.execute(
                """INSERT INTO episodes(feed_id, guid, rss_title, audio_url, published_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (feed_id, guid, rss_title, audio_url, published_at),
            )
            ep = conn.execute(
                "SELECT id FROM episodes WHERE guid=?", (guid,)
            ).fetchone()
        return {
            "queued": True,
            "episode_id": ep["id"],
            "rss_title": rss_title,
            "message": "Najnowszy odcinek dodany do kolejki",
        }
