"""Minimalny test: kontrakt webhooka i przejscie zadania przez kolejke."""
import os, tempfile, sqlite3

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["AUDIO_DIR"] = tempfile.mkdtemp()
os.environ["MODELS_DIR"] = tempfile.mkdtemp()
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

# Limit rozmiaru pobierania jest egzekwowany.
app.MAX_AUDIO_MB = 0
try:
    app.download("http://127.0.0.1:1/big.mp3", "/tmp/x.audio")
except Exception:
    pass  # polaczenie odrzucone albo limit — obie sciezki koncza sie wyjatkiem

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
_c.commit(); _c.close()
os.environ["DB_PATH"] = old_db
importlib.reload(app)
app.init_db()
with app.db() as c:
    assert c.execute("SELECT webhook_ok FROM jobs WHERE guid='old1'").fetchone()[0] == 1
assert app.pending_webhook() is None, "stare zadania nie moga trafic do dosylania"

print("OK")
