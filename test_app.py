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

print("OK")
