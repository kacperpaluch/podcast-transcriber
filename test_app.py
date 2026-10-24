"""Minimalny test: kontrakt webhooka i przejscie zadania przez kolejke."""
import os, tempfile, sqlite3

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["AUDIO_DIR"] = tempfile.mkdtemp()
os.environ["MODELS_DIR"] = tempfile.mkdtemp()
os.environ["TRANSCRIPTS_DIR"] = TXT = tempfile.mkdtemp()
import app

app.init_db()
with app.db() as c:
    c.execute("INSERT INTO jobs(audio_url,guid,episode_title,language,transcript,"
              "duration_seconds,status) VALUES('http://x/a.mp3','g1','Tytul','pl','tekst',60,'done')")
    row = c.execute("SELECT * FROM jobs WHERE guid='g1'").fetchone()

p = app.payload_for(row)
assert p["event"] == "transcription_completed"
assert p["transcript"] == "tekst" and p["episode_title"] == "Tytul"
assert set(p) == {"event", "feed_name", "rss_feed_title", "feed_url", "episode_title",
                  "guid", "audio_url", "published_at", "language", "duration_seconds",
                  "transcript"}, "kontrakt z n8n sie zmienil"

# Restart musi wrocic zadania 'running' do kolejki, zeby nie przepadly.
with app.db() as c:
    c.execute("INSERT INTO jobs(audio_url,guid,status) VALUES('http://x/b.mp3','g2','running')")
app.init_db()
with app.db() as c:
    assert c.execute("SELECT status FROM jobs WHERE guid='g2'").fetchone()[0] == "queued"
# Start dopisuje pliki .txt dla gotowych transkryptow, ktorych jeszcze nie ma na dysku.
assert open(os.path.join(TXT, "1 - Tytul.txt"), encoding="utf-8").read() == "tekst"

# Zadanie, ktore ubilo proces MAX_ATTEMPTS razy, nie moze wracac do kolejki
# w nieskonczonosc — inaczej OOM restartuje aplikacje bez konca.
with app.db() as c:
    c.execute("INSERT INTO jobs(audio_url,guid,status,attempts) "
              "VALUES('http://x/c.mp3','g3','running',?)", (app.MAX_ATTEMPTS,))
app.init_db()
with app.db() as c:
    assert c.execute("SELECT status FROM jobs WHERE guid='g3'").fetchone()[0] == "error"

# Webhook: nieudana proba planuje kolejna zamiast przepasc; udana zamyka temat.
os.environ["WEBHOOK_URL"] = "http://127.0.0.1:1/nope"  # port 1 = pewna odmowa
import importlib; importlib.reload(app)
app.init_db()
with app.db() as c:
    c.execute("INSERT INTO jobs(audio_url,guid,status,transcript) "
              "VALUES('http://x/d.mp3','g4','done','tekst')")
    jid = c.execute("SELECT id FROM jobs WHERE guid='g4'").fetchone()[0]

# Zadania z wczesniejszych sekcji testu nie moga mieszac sie do kolejki dosylania.
with app.db() as c:
    c.execute("UPDATE jobs SET webhook_ok=1 WHERE id<>?", (jid,))

app.try_webhook(jid)
with app.db() as c:
    r = c.execute("SELECT webhook_ok,webhook_attempts,webhook_after FROM jobs WHERE id=?",
                  (jid,)).fetchone()
assert r["webhook_ok"] == 0 and r["webhook_attempts"] == 1, dict(r)
assert r["webhook_after"] > 0, "kolejna proba musi byc zaplanowana"

# Zaplanowana w przyszlosc — petla nie moze jej brac od razu (inaczej busy-loop).
assert app.pending_webhook() is None
with app.db() as c:
    c.execute("UPDATE jobs SET webhook_after=0 WHERE id=?", (jid,))
assert app.pending_webhook()["id"] == jid

# Po wyczerpaniu limitu zadanie znika z kolejki dosylania.
with app.db() as c:
    c.execute("UPDATE jobs SET webhook_attempts=? WHERE id=?", (app.WEBHOOK_MAX_ATTEMPTS, jid))
assert app.pending_webhook() is None

# Lokalny serwer: GET oddaje 2 MB, POST odpowiada 200 albo 301 zaleznie od sciezki.
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", str(2 << 20))
        self.end_headers(); self.wfile.write(b"\0" * (2 << 20))
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(301 if self.path == "/redir" else 200)
        self.send_header("Location", "/ok"); self.send_header("Content-Length", "0")
        self.end_headers()

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"

def hook(path):
    app.WEBHOOK_URL = base + path
    with app.db() as c:
        c.execute("UPDATE jobs SET webhook_attempts=0, webhook_after=0 WHERE id=?", (jid,))
    app.try_webhook(jid)
    with app.db() as c:
        return c.execute("SELECT webhook_ok,error FROM jobs WHERE id=?", (jid,)).fetchone()

# Przekierowanie to nie dostarczenie — n8n niczego nie dostal.
assert hook("/redir")["webhook_ok"] == 0
r = hook("/ok")
assert r["webhook_ok"] == 1 and r["error"] is None, dict(r)

# Limit rozmiaru pobierania jest egzekwowany.
audio = os.path.join(tempfile.mkdtemp(), "x.audio")
app.MAX_AUDIO_MB = 1
try:
    app.download(base + "/big.mp3", audio)
    raise AssertionError("2 MB przeszlo przy limicie 1 MB")
except ValueError:
    pass
app.MAX_AUDIO_MB = 3
app.download(base + "/big.mp3", audio)
assert os.path.getsize(audio) == 2 << 20

# Zadanie zakonczone bledem mozna zglosic ponownie; kazde inne daje 409.
with app.db() as c:
    c.execute("INSERT INTO jobs(audio_url,guid,status,error,attempts) "
              "VALUES('http://x/e.mp3','g5','error','boom',3)")
app.queue(app.Job(audio_url="http://x/e2.mp3", guid="g5"))
with app.db() as c:
    r = c.execute("SELECT status,error,attempts,audio_url FROM jobs WHERE guid='g5'").fetchone()
assert tuple(r) == ("queued", None, 0, "http://x/e2.mp3"), dict(r)
try:
    app.queue(app.Job(audio_url="http://x/e2.mp3", guid="g5"))
    raise AssertionError("duplikat guid w kolejce musi dac 409")
except app.HTTPException as e:
    assert e.status_code == 409

# Aktualizacja starej bazy nie moze wywolac lawiny powtorzonych webhookow.
import sqlite3 as _s
old_db = os.path.join(tempfile.mkdtemp(), "old.db")
_c = _s.connect(old_db)
_c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, audio_url TEXT NOT NULL,"
           " guid TEXT UNIQUE, episode_title TEXT, feed_name TEXT, feed_url TEXT,"
           " rss_feed_title TEXT, published_at TEXT, language TEXT, status TEXT DEFAULT 'queued',"
           " duration_seconds INTEGER, progress_seconds INTEGER, transcript TEXT, error TEXT,"
           " attempts INTEGER DEFAULT 0, created_at TEXT)")
_c.execute("INSERT INTO jobs(audio_url,guid,status,transcript) VALUES('u','old1','done','t')")
_c.execute("INSERT INTO jobs(audio_url,guid,status,transcript,error) "
           "VALUES('u','old2','done','t','webhook nieudany')")
_c.commit(); _c.close()
os.environ["DB_PATH"] = old_db
importlib.reload(app)
app.init_db()
with app.db() as c:
    assert c.execute("SELECT webhook_ok FROM jobs WHERE guid='old1'").fetchone()[0] == 1
# ...poza tymi, ktorych webhook pod starym kodem sie nie udal — te trzeba doslac.
with app.db() as c:
    old2 = c.execute("SELECT id FROM jobs WHERE guid='old2'").fetchone()[0]
    plan = " ".join(r[3] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE status='done' AND webhook_ok=0 "
        "AND webhook_attempts < 10 AND webhook_after <= 1 ORDER BY id LIMIT 1"))
assert app.pending_webhook()["id"] == old2, "tylko nieudany stary webhook idzie do dosylania"
assert "jobs_webhook_pending" in plan, plan

# UI: kazdy stan sie renderuje, tresc z n8n jest escapowana, a link do audio
# z innym schematem niz http(s) nie trafia do href.
with app.db() as c:
    c.execute("INSERT INTO jobs(audio_url,guid,episode_title,status,transcript,webhook_ok,"
              "created_at) VALUES('javascript:alert(1)','u1','<b>x</b>','done','a\nb',1,"
              "datetime('now'))")
    c.execute("INSERT INTO jobs(audio_url,guid,status,duration_seconds,progress_seconds) "
              "VALUES('http://x/odc%201.mp3','u2','running',3600,900)")
    ui = c.execute("SELECT id FROM jobs WHERE guid='u1'").fetchone()[0]
    run = c.execute("SELECT id FROM jobs WHERE guid='u2'").fetchone()[0]
home = app.index().body.decode()
assert "&lt;b&gt;x&lt;/b&gt;" in home and "<b>x</b>" not in home
assert "odc 1.mp3" in home and "25%" in home, "brak tytulu = nazwa pliku; postep w %"
detail = app.job_page(ui).body.decode()
assert "javascript:" not in detail and "<p>a b</p>" in detail
assert "data-live" in app.job_page(run).body.decode()
assert app.job_page(10**6).status_code == 404
with app.db() as c:
    app.save_txt(c.execute("SELECT * FROM jobs WHERE id=?", (ui,)).fetchone())
assert os.path.exists(os.path.join(TXT, f"{ui} - _b_x__b_.txt")), os.listdir(TXT)

# Webhook po wyczerpaniu prob mozna wyslac od nowa; dostarczonego nie rusza.
with app.db() as c:
    c.execute("UPDATE jobs SET webhook_ok=0, webhook_attempts=?, error='x' WHERE id=?",
              (app.WEBHOOK_MAX_ATTEMPTS, ui))
assert "Wyślij webhook ponownie" in app.job_page(ui).body.decode()
assert app.resend_webhook(ui).status_code == 303
with app.db() as c:
    r = c.execute("SELECT webhook_ok,webhook_attempts,webhook_after,error FROM jobs WHERE id=?",
                  (ui,)).fetchone()
assert tuple(r) == (0, 0, 0, None), dict(r)

# Literowka w MODEL laduje model domyslny zamiast blokowac kolejke.
import faster_whisper
class FakeModel:
    def __init__(self, name, **kw):
        if name != app.DEFAULT_MODEL:
            raise ValueError("Invalid model size")
        self.name = name
faster_whisper.WhisperModel, app.MODEL = FakeModel, "large-v3-trubo"
assert app.load_model().name == app.DEFAULT_MODEL
assert "MODEL=large-v3-trubo" in app.model_note and "MODEL=large-v3-trubo" in app.index().body.decode()
app.MODEL = app.DEFAULT_MODEL
app.load_model()
assert app.model_note == ""

print("OK")
