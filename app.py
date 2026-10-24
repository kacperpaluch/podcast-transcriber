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
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "/data/app.db")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
MODELS_DIR = os.environ.get("MODELS_DIR", "/data/models")
TRANSCRIPTS_DIR = os.environ.get("TRANSCRIPTS_DIR", "/data/transcripts")
DEFAULT_MODEL = "large-v3-turbo"
MODEL = os.environ.get("MODEL", DEFAULT_MODEL)
CPU_THREADS = int(os.environ.get("CPU_THREADS", "4"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
IDLE_UNLOAD_SECS = int(os.environ.get("IDLE_UNLOAD_SECS", "300"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
MAX_AUDIO_MB = int(os.environ.get("MAX_AUDIO_MB", "1000"))
WEBHOOK_MAX_ATTEMPTS = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "10"))
# Odstepy miedzy kolejnymi probami webhooka; ostatnia wartosc powtarza sie.
WEBHOOK_DELAYS = [5, 15, 30, 60, 120, 300, 900, 3600]

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
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audio_url TEXT NOT NULL, guid TEXT UNIQUE,
            episode_title TEXT, feed_name TEXT, feed_url TEXT, rss_feed_title TEXT,
            published_at TEXT, language TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            duration_seconds INTEGER, progress_seconds INTEGER DEFAULT 0,
            transcript TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            webhook_ok INTEGER NOT NULL DEFAULT 0,
            webhook_attempts INTEGER NOT NULL DEFAULT 0,
            webhook_after REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        migrated = False
        for col, decl in (("attempts", "INTEGER NOT NULL DEFAULT 0"),
                          ("webhook_ok", "INTEGER NOT NULL DEFAULT 0"),
                          ("webhook_attempts", "INTEGER NOT NULL DEFAULT 0"),
                          ("webhook_after", "REAL NOT NULL DEFAULT 0")):
            try:  # migracja baz sprzed dodania kolumny
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
                migrated = migrated or col == "webhook_ok"
            except sqlite3.OperationalError:
                pass
        # Zadania zakonczone przed wprowadzeniem kolejki webhookow maja swoje
        # webhooki juz wyslane — bez tego aktualizacja zalalaby n8n powtorkami.
        # Wyjatek: te, ktore stary kod oznaczyl jako nieudane, trzeba doslac.
        if migrated:
            c.execute("UPDATE jobs SET webhook_ok=1 WHERE status='done' AND error IS NULL")
        # Bez indeksu szukanie zaleglych webhookow czyta transkrypt kazdego zadania.
        c.execute("CREATE INDEX IF NOT EXISTS jobs_webhook_pending ON jobs(webhook_after) "
                  "WHERE status='done' AND webhook_ok=0")
        # Po restarcie dokończ to, co było w locie — ale zadanie, które ubiło
        # proces MAX_ATTEMPTS razy (np. przez OOM), odkłada się na bok zamiast
        # restartować aplikację w nieskończoność.
        c.execute("UPDATE jobs SET status='error', error='przekroczono limit prób' "
                  "WHERE status='running' AND attempts >= ?", (MAX_ATTEMPTS,))
        c.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        # Transkrypty sprzed zapisu do plikow tez maja trafic na dysk.
        for row in c.execute("SELECT id, audio_url, episode_title, transcript FROM jobs "
                             "WHERE status='done' AND transcript IS NOT NULL"):
            if not os.path.exists(txt_path(row)):
                save_txt(row)


def txt_path(row) -> str:
    # Znaki zakazane w nazwach plikow na Windows/SMB, zeby katalog dalo sie udostepnic.
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name(row))[:120].strip(" .")
    return os.path.join(TRANSCRIPTS_DIR, f"{row['id']} - {safe}.txt")


def save_txt(row):
    """Kopia transkryptu w pliku. Baza zostaje zrodlem prawdy, wiec blad zapisu
    tylko loguje — nie moze zepsuc zadania ani wysylki webhooka."""
    path = txt_path(row)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(row["transcript"])
        os.replace(path + ".tmp", path)
    except OSError:
        log.exception("Nie zapisałem %s", path)


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


def try_webhook(job_id: int):
    """Jedna proba wyslania. Bez sleep — nieudana wysylka nie moze blokowac
    kolejki, wiec kolejna proba jest planowana w bazie i podejmie ja petla."""
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not WEBHOOK_URL:
        log.warning("Brak WEBHOOK_URL — pomijam wysyłkę")
        with db() as c:
            c.execute("UPDATE jobs SET webhook_ok=1 WHERE id=?", (job_id,))
        return
    n = row["webhook_attempts"] + 1
    try:
        r = httpx.post(WEBHOOK_URL, json=payload_for(row), timeout=30)
        if r.is_success:
            log.info("Webhook OK (%s) dla zadania %d", r.status_code, job_id)
            with db() as c:
                c.execute("UPDATE jobs SET webhook_ok=1, webhook_attempts=?, error=NULL "
                          "WHERE id=?", (n, job_id))
            return
        why = f"HTTP {r.status_code}"
    except Exception as e:
        why = str(e)[:200]

    delay = WEBHOOK_DELAYS[min(n - 1, len(WEBHOOK_DELAYS) - 1)]
    if n >= WEBHOOK_MAX_ATTEMPTS:
        log.error("Webhook poddaje sie po %d probach (%s), zadanie %d", n, why, job_id)
        err = f"webhook nieudany po {n} próbach: {why}"
        nxt = 0.0
    else:
        log.warning("Webhook nieudany (%s), proba %d, ponowie za %ds", why, n, delay)
        err = f"webhook: {why}"
        nxt = time.time() + delay
    with db() as c:
        c.execute("UPDATE jobs SET webhook_attempts=?, webhook_after=?, error=? WHERE id=?",
                  (n, nxt, err, job_id))


def pending_webhook():
    """Zadanie gotowe, ktorego webhook jeszcze nie doszedl i nadszedl czas proby."""
    with db() as c:
        return c.execute(
            "SELECT id FROM jobs WHERE status='done' AND webhook_ok=0 "
            "AND webhook_attempts < ? AND webhook_after <= ? ORDER BY id LIMIT 1",
            (WEBHOOK_MAX_ATTEMPTS, time.time())).fetchone()


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


def download(url: str, path: str):
    limit = MAX_AUDIO_MB * 1024 * 1024
    log.info("Pobieram %s", url)
    size = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"plik przekracza MAX_AUDIO_MB={MAX_AUDIO_MB}")
                f.write(chunk)
    log.info("Pobrano %.1f MB", size / 1_048_576)


def process(model, job_id: int):
    with db() as c:
        c.execute("UPDATE jobs SET status='running', attempts=attempts+1 WHERE id=?", (job_id,))
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    path = os.path.join(AUDIO_DIR, f"job_{job_id}.audio")
    try:  # obejmuje pobieranie, zeby zerwane polaczenie nie zostawialo ogona na dysku
        download(row["audio_url"], path)
        log.info("Transkrybuję zadanie %d", job_id)
        text = transcribe(model, path, job_id, row["language"])
    finally:
        if os.path.exists(path):
            os.remove(path)

    with db() as c:
        c.execute("UPDATE jobs SET transcript=?, status='done' WHERE id=?", (text, job_id))
    save_txt({**row, "transcript": text})
    log.info("Gotowe: zadanie %d, %d znaków", job_id, len(text))


model_note = ""  # niepusty, gdy MODEL z compose sie nie zaladowal; pokazywany w UI


def load_model():
    """Literowka w MODEL nie moze zatrzymac kolejki: wtedy laduje sie DEFAULT_MODEL,
    a UI mowi o tym w naglowku. Kolejne zaladowanie (po zwolnieniu) znow probuje MODEL."""
    global model_note
    from faster_whisper import WhisperModel
    for m in dict.fromkeys((MODEL, DEFAULT_MODEL)):
        log.info("Ładuję model %s (int8, %d wątki)", m, CPU_THREADS)
        try:
            model = WhisperModel(m, device="cpu", compute_type="int8",
                                 download_root=MODELS_DIR, cpu_threads=CPU_THREADS)
        except Exception as e:
            if m == DEFAULT_MODEL:
                raise
            log.exception("Nie mogę załadować modelu %s, przechodzę na %s", m, DEFAULT_MODEL)
            why = str(e)[:200]
            continue
        model_note = "" if m == MODEL else \
            f"MODEL={MODEL} się nie ładuje ({why}), używam {DEFAULT_MODEL}"
        return model


def worker():
    """Model ładowany na żądanie i zwalniany po IDLE_UNLOAD_SECS bezczynności —
    bezczynna aplikacja zajmuje ~100 MB zamiast ~1,5 GB. Ładowanie z cache
    trwa ~3 s, więc przy serii odcinków płaci się za nie raz."""
    model, idle_since = None, time.time()
    while True:
        try:
            # Webhooki ida miedzy zadaniami, zeby dluga kolejka ich nie wstrzymywala.
            late = pending_webhook()
            if late:
                try_webhook(late["id"])
            with db() as c:
                row = c.execute(
                    "SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                if late:
                    continue
                if model is not None and time.time() - idle_since > IDLE_UNLOAD_SECS:
                    log.info("Kolejka pusta — zwalniam model")
                    model = None
                    gc.collect()
                time.sleep(5)
                continue
            if model is None:
                model = load_model()
            try:
                process(model, row["id"])
            except Exception as e:
                log.exception("Zadanie %d nieudane", row["id"])
                with db() as c:
                    c.execute("UPDATE jobs SET status='error', error=? WHERE id=?",
                              (str(e)[:500], row["id"]))
            idle_since = time.time()
        except Exception:
            # Watek jest jedyny — blad bazy czy pobierania modelu nie moze go ubic.
            log.exception("Błąd pętli roboczej, ponawiam za 5 s")
            time.sleep(5)


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
            # Zadanie zakonczone bledem wraca do kolejki zamiast blokowac swoj guid.
            done = c.execute(
                f"UPDATE jobs SET status='queued', error=NULL, attempts=0, progress_seconds=0, "
                f"{','.join(f + '=?' for f in FIELDS)} WHERE guid=? AND status='error'",
                vals + [job.guid]).rowcount
            if not done:
                raise HTTPException(409, f"guid '{job.guid}' już istnieje")
            return {"job_id": c.execute("SELECT id FROM jobs WHERE guid=?",
                                        (job.guid,)).fetchone()["id"]}
    return {"job_id": cur.lastrowid}


@app.get("/api/jobs/{job_id}")
def status(job_id: int):
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "nie ma takiego zadania")
    return dict(row)


# --- UI: podgląd kolejki i transkryptów, renderowany na serwerze ---

MONTHS = ("sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru")
LIST_COLS = ("id, audio_url, episode_title, feed_name, status, duration_seconds, "
             "progress_seconds, error, webhook_ok, webhook_attempts, created_at")

CSS = """
:root{color-scheme:light;--bg:#f6f7f8;--fg:#16191d;--muted:#5c636c;--line:#e1e4e8;
--soft:#eceef1;--accent:#16705a;--err:#b3261e;--warn:#8a5600;
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#111315;--fg:#e7e9ec;
--muted:#9198a1;--line:#272b30;--soft:#1d2024;--accent:#55c4a1;--err:#f08b80;--warn:#e3aa4c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
::selection{background:color-mix(in srgb,var(--accent) 28%,transparent)}
.wrap{max-width:46rem;margin:0 auto;padding:1.25rem 1rem 4rem}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:.25rem 1rem;
padding-bottom:1rem;margin-bottom:2rem;border-bottom:1px solid var(--line)}
.brand{font-weight:600;text-decoration:none}
.muted,.meta{color:var(--muted)}
.meta{font-size:.8125rem;margin:.125rem 0 0}
.num{font-variant-numeric:tabular-nums}
a{color:inherit;text-decoration-thickness:1px;text-underline-offset:.2em}
a.title{font-weight:500;text-decoration:none;overflow-wrap:anywhere}
a.title:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}
h1{font-size:1.5rem;line-height:1.3;letter-spacing:-.01em;margin:0 0 .375rem;text-wrap:balance;
overflow-wrap:anywhere}
h2{font-size:.8125rem;font-weight:600;color:var(--muted);margin:2.75rem 0 .75rem}
section:first-child h2{margin-top:0}
p{margin:0}
.current .title{font-size:1.125rem}
.bar{height:4px;margin:1rem 0 .5rem;background:var(--soft);border-radius:2px;overflow:hidden}
.bar span{display:block;height:100%;background:var(--accent)}
.times{display:flex;justify-content:space-between;gap:1rem;font-size:.8125rem;color:var(--muted)}
.list{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
.list li{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;
padding:.75rem 0;border-bottom:1px solid var(--line)}
.list li>div{min-width:0}
.more{font-size:.8125rem;color:var(--muted);margin-top:.75rem}
.st{display:inline-flex;align-items:center;gap:.4rem;flex:none;font-size:.8125rem;color:var(--muted);
white-space:nowrap}
.st::before{content:"";width:.4375rem;height:.4375rem;border-radius:50%;background:currentColor}
.st.run{color:var(--accent)}.st.err{color:var(--err)}.st.warn{color:var(--warn)}
.st.wait::before{background:none;box-shadow:inset 0 0 0 1.5px currentColor}
.why{font-size:.8125rem;margin-top:.25rem;overflow-wrap:anywhere}
.why.err{color:var(--err)}.why.warn{color:var(--warn)}
header .why{flex-basis:100%;margin:0}
.idle{color:var(--muted)}
.notice{margin:1.5rem 0;padding:.875rem 1rem;background:var(--soft);border-radius:6px;font-size:.875rem;
overflow-wrap:anywhere}
.notice strong{font-weight:600}
code{font:.8125rem ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);
padding:.1em .35em;border-radius:3px}
.notice code,.notice button{background:var(--bg)}
.back{display:inline-flex;align-items:center;gap:.375rem;margin-bottom:1.5rem;font-size:.875rem;
color:var(--muted);text-decoration:none}
.back:hover{color:var(--fg)}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem 1.25rem;margin:1.5rem 0 2rem;
font-size:.875rem}
.actions a{color:var(--muted)}.actions a:hover{color:var(--fg)}
button{font:inherit;font-size:.875rem;color:var(--fg);background:none;border:1px solid var(--line);
border-radius:6px;padding:.375rem .875rem;cursor:pointer}
button:hover{border-color:var(--muted)}
button:active{background:var(--soft)}
.transcript{max-width:68ch;font-size:1rem;line-height:1.7}
.transcript p+p{margin-top:1em}
.empty{padding:1rem 0;color:var(--muted)}
.empty p+p{margin-top:.5rem}
@media (max-width:32rem){.list li{flex-direction:column;gap:.25rem}h1{font-size:1.3125rem}}
"""

# Odświeża <main> co 5 s, dopóki strona ma coś, co się zmienia. Nie rusza strony,
# gdy karta jest w tle albo gdy fokus klawiatury siedzi w treści.
JS = """
const live=()=>document.querySelector('[data-live]');
if(live()){const t=setInterval(async()=>{
 if(!live())return clearInterval(t);
 const m=document.querySelector('main'),a=document.activeElement;
 if(document.hidden||(a&&a!==document.body&&m.contains(a)))return;
 try{const d=new DOMParser().parseFromString(await(await fetch(location.href)).text(),'text/html');
  m.replaceWith(d.querySelector('main'));document.title=d.title}catch(e){}},5000)}
document.addEventListener('click',e=>{const b=e.target.closest('[data-copy]');if(!b)return;
 const text=document.querySelector(b.dataset.copy).innerText;
 const done=()=>{b.textContent='Skopiowano';setTimeout(()=>b.textContent='Kopiuj transkrypt',2000)};
 // navigator.clipboard działa tylko na https/localhost, a UI zwykle otwiera się po IP w LAN.
 if(navigator.clipboard&&isSecureContext)return navigator.clipboard.writeText(text).then(done);
 const ta=Object.assign(document.createElement('textarea'),{value:text});
 document.body.append(ta);ta.select();document.execCommand('copy');ta.remove();done()});
"""

ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
        "%3Ccircle cx='8' cy='8' r='6' fill='%2316705a'/%3E%3C/svg%3E")
BACK = ('<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
        'stroke-width="1.5" aria-hidden="true"><path d="M10 3 5 8l5 5"/></svg>')


def page(title: str, body: str, live: bool, status: int = 200) -> HTMLResponse:
    note = f'<p class="why warn">{html.escape(model_note)}</p>' if model_note else ""
    return HTMLResponse(
        f'<!doctype html><html lang=pl><meta charset=utf-8>'
        f'<meta name=viewport content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(title)}</title><link rel=icon href="{ICON}"><style>{CSS}</style>'
        f'<div class=wrap><header><a class=brand href="/">Podcast Transcriber</a>'
        f'<span class="meta">{html.escape(MODEL)} · CPU</span>'
        f'{note}</header>'
        f'<main{" data-live" if live else ""}>{body}</main></div><script>{JS}</script>',
        status_code=status)


def when(ts: str) -> str:
    """created_at z SQLite jest w UTC; pokazujemy czas strefy kontenera (TZ)."""
    if not ts:
        return ""
    t = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).astimezone()
    days = (datetime.now().astimezone().date() - t.date()).days
    day = "dziś" if days == 0 else "wczoraj" if days == 1 else f"{t.day} {MONTHS[t.month - 1]}"
    return f"{day}, {t:%H:%M}"


def clock(s) -> str:
    h, rest = divmod(int(s or 0), 3600)
    return f"{h}:{rest // 60:02}:{rest % 60:02}" if h else f"{rest // 60}:{rest % 60:02}"


def minutes(s) -> str:
    m = max(round(s / 60), 1)
    return f"{m // 60} h {m % 60:02} min" if m >= 60 else f"{m} min"


def name(r) -> str:
    """Tytuł odcinka, a bez niego nazwa pliku z URL — lepsze niż pusty wiersz."""
    if r["episode_title"]:
        return r["episode_title"]
    tail = urlsplit(r["audio_url"]).path.rsplit("/", 1)[-1]
    return unquote(tail) or f"Zadanie #{r['id']}"


def state(r) -> tuple[str, str]:
    """(etykieta, klasa). 'Gotowe' bez dostarczonego webhooka to dla n8n jeszcze nie koniec."""
    s = r["status"]
    if s == "running":
        return ("Transkrypcja", "run") if r["duration_seconds"] else ("Pobieranie audio", "run")
    if s == "queued":
        return "W kolejce", "wait"
    if s == "error":
        return "Błąd", "err"
    if not r["webhook_ok"]:
        if r["webhook_attempts"] >= WEBHOOK_MAX_ATTEMPTS:
            return "Webhook nie doszedł", "warn"
        return "Wysyłanie webhooka", "wait"
    return "Gotowe", "done"


def meta(*parts) -> str:
    return " · ".join(html.escape(str(p)) for p in parts if p)


def progress(r) -> str:
    if not r["duration_seconds"]:
        return '<p class="times"><span>Pobieranie pliku audio…</span></p>'
    done, total = r["progress_seconds"] or 0, r["duration_seconds"]
    pct = min(100 * done // total, 100)
    return (f'<div class=bar role=progressbar aria-label="Postęp transkrypcji" aria-valuemin=0 '
            f'aria-valuemax=100 aria-valuenow={pct}><span style="width:{pct}%"></span></div>'
            f'<p class="times num"><span>{clock(done)} / {clock(total)}</span><span>{pct}%</span></p>')


def link(r) -> str:
    return f'<a class=title href="/jobs/{r["id"]}">{html.escape(name(r))}</a>'


@app.get("/", response_class=HTMLResponse)
def index():
    with db() as c:
        cur = c.execute(f"SELECT {LIST_COLS} FROM jobs WHERE status='running' "
                        "ORDER BY id LIMIT 1").fetchone()
        waiting = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
        nxt = c.execute(f"SELECT {LIST_COLS} FROM jobs WHERE status='queued' "
                        "ORDER BY id LIMIT 5").fetchall()
        # Bez kolumny transcript — 50 godzinnych transkryptów to kilka MB na każde odświeżenie.
        rows = c.execute(f"SELECT {LIST_COLS} FROM jobs WHERE status IN ('done','error') "
                         "ORDER BY id DESC LIMIT 50").fetchall()

    if not (cur or nxt or rows):
        return page("Podcast Transcriber", (
            '<div class=empty><p>Nie ma jeszcze żadnych zadań.</p>'
            '<p>Zadania dodaje n8n przez <code>POST /api/transcribe</code> z polem '
            '<code>audio_url</code>. Pojawią się tutaj same, strona odświeża się co kilka sekund.</p>'
            '</div>'), live=True)

    if cur:
        now = (f'<div class=current>{link(cur)}<p class=meta>{meta(cur["feed_name"])}</p>'
               f'{progress(cur)}</div>')
    else:
        now = (f'<p class=idle>Nic się teraz nie transkrybuje. Model zwalnia pamięć po '
               f'{minutes(IDLE_UNLOAD_SECS)} bezczynności.</p>')
    body = f'<section><h2>Teraz</h2>{now}</section>'

    if nxt:
        items = "".join(f'<li><div>{link(r)}<p class=meta>{meta(r["feed_name"], when(r["created_at"]))}'
                        f'</p></div></li>' for r in nxt)
        more = f'<p class=more>i {waiting - len(nxt)} więcej</p>' if waiting > len(nxt) else ""
        body += f'<section><h2>W kolejce <span class=num>({waiting})</span></h2>' \
                f'<ul class=list>{items}</ul>{more}</section>'

    if rows:
        items = ""
        for r in rows:
            label, cls = state(r)
            why = f'<p class="why {cls}">{html.escape(r["error"])}</p>' if r["error"] else ""
            dur = minutes(r["duration_seconds"]) if r["duration_seconds"] else None
            items += (f'<li><div>{link(r)}<p class=meta>{meta(r["feed_name"], dur, when(r["created_at"]))}'
                      f'</p>{why}</div><span class="st {cls}">{label}</span></li>')
        body += f'<section><h2>Zakończone</h2><ul class=list>{items}</ul></section>'

    title = f'{100 * (cur["progress_seconds"] or 0) // cur["duration_seconds"]}% · ' \
        if cur and cur["duration_seconds"] else ""
    return page(f"{title}Podcast Transcriber", body, live=True)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(job_id: int):
    with db() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        ahead = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND id<?",
                          (job_id,)).fetchone()[0]
    back = f'<a class=back href="/">{BACK}Wszystkie zadania</a>'
    if not r:
        return page("Nie ma takiego zadania", f'{back}<h1>Nie ma takiego zadania</h1>'
                    f'<p class=muted>Zadanie #{job_id} nie istnieje albo zostało usunięte z bazy.</p>',
                    live=False, status=404)

    label, cls = state(r)
    text = r["transcript"] or ""
    words = f"{len(text.split()):,} słów".replace(",", " ") if text else None
    pub = (r["published_at"] or "")[:10]
    body = (f'{back}<h1>{html.escape(name(r))}</h1>'
            f'<p class=meta>{meta(r["feed_name"], pub, r["language"], minutes(r["duration_seconds"]) if r["duration_seconds"] else None, words)}</p>'
            f'<p class=meta style="margin-top:.75rem"><span class="st {cls}">{label}</span></p>')

    s, err = r["status"], html.escape(r["error"] or "")
    if s == "running":
        body += progress(r)
    elif s == "queued":
        body += (f'<div class=notice>Czeka w kolejce. '
                 f'{"Przed nim: " + str(ahead) + "." if ahead else "Jest następne."}</div>')
    elif s == "error":
        retry = (f'wyślij je jeszcze raz z tym samym <code>guid</code>, a wróci do kolejki'
                 if r["guid"] else "wyślij je jeszcze raz")
        body += (f'<div class=notice><strong>Transkrypcja się nie udała.</strong> {err}<br>'
                 f'Żeby spróbować ponownie, {retry}.</div>')
    elif not r["webhook_ok"]:
        why = f' {err}' if err else ""
        body += (f'<div class=notice><strong>n8n jeszcze nie odebrał transkryptu.</strong>{why}<br>'
                 f'Tekst jest bezpieczny w bazie i w pliku .txt.'
                 f'<form method=post action="/jobs/{r["id"]}/webhook" style="margin-top:.75rem">'
                 f'<button>Wyślij webhook ponownie</button></form></div>')

    if s == "done":
        audio = r["audio_url"]
        src = f'<a href="{html.escape(audio)}" rel=noreferrer>Oryginalne audio</a>' \
            if audio.startswith(("http://", "https://")) else ""
        if text:
            lines = text.split("\n")
            # shortcut: akapity co 6 segmentów whispera, nie po sensie; lepsze wymagałoby modelu.
            paras = "".join(f'<p>{html.escape(" ".join(lines[i:i + 6]))}</p>'
                            for i in range(0, len(lines), 6))
            body += (f'<div class=actions><button type=button data-copy="#transcript">'
                     f'Kopiuj transkrypt</button>{src}<a href="/api/jobs/{r["id"]}">JSON</a></div>'
                     f'<article id=transcript class=transcript lang="{html.escape(r["language"] or "")}">'
                     f'{paras}</article>')
        else:
            body += (f'<div class=notice>Transkrypt jest pusty: w nagraniu nie wykryto mowy.</div>'
                     f'<div class=actions>{src}</div>')

    sending = s == "done" and not r["webhook_ok"] and r["webhook_attempts"] < WEBHOOK_MAX_ATTEMPTS
    return page(f"{name(r)} · Podcast Transcriber", body,
                live=s in ("queued", "running") or sending)


@app.post("/jobs/{job_id}/webhook")
def resend_webhook(job_id: int):
    """Po wyczerpaniu prob (np. n8n lezal dluzej niz kilka godzin) zaczyna liczyc od nowa;
    wysyla petla robocza, jak kazdy inny webhook."""
    with db() as c:
        c.execute("UPDATE jobs SET webhook_ok=0, webhook_attempts=0, webhook_after=0, error=NULL "
                  "WHERE id=? AND status='done' AND webhook_ok=0", (job_id,))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
