import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/app.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                rss_feed_title TEXT,
                baseline_established INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER REFERENCES feeds(id) ON DELETE CASCADE,
                guid TEXT NOT NULL UNIQUE,
                rss_title TEXT,
                audio_url TEXT NOT NULL,
                published_at TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                transcript TEXT,
                language TEXT,
                duration_seconds INTEGER,
                transcribed_seconds INTEGER,
                error TEXT,
                feed_name TEXT,
                feed_url TEXT,
                rss_feed_title TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
            CREATE INDEX IF NOT EXISTS idx_episodes_feed_id ON episodes(feed_id);

            CREATE TABLE IF NOT EXISTS webhook_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                sent_at TEXT NOT NULL DEFAULT (datetime('now')),
                status_code INTEGER,
                ok INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_webhook_log_episode ON webhook_log(episode_id);

            INSERT OR IGNORE INTO settings(key, value) VALUES ('whisper_model', 'large-v3-turbo');
            INSERT OR IGNORE INTO settings(key, value) VALUES ('webhook_url', '');
        """)

        # Migration: add language column to feeds if missing
        feeds_cols = [r["name"] for r in conn.execute("PRAGMA table_info(feeds)").fetchall()]
        if "language" not in feeds_cols:
            conn.execute("ALTER TABLE feeds ADD COLUMN language TEXT")

        # Migration: make episodes.feed_id nullable + add external metadata columns
        ep_cols = {c["name"]: c for c in conn.execute("PRAGMA table_info(episodes)").fetchall()}
        if ep_cols.get("feed_id", {}).get("notnull", 0) == 1:
            # SQLite can't ALTER COLUMN — recreate table without NOT NULL on feed_id
            conn.executescript("""
                PRAGMA foreign_keys=OFF;
                ALTER TABLE episodes RENAME TO _episodes_v1;
                CREATE TABLE episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feed_id INTEGER REFERENCES feeds(id) ON DELETE CASCADE,
                    guid TEXT NOT NULL UNIQUE,
                    rss_title TEXT,
                    audio_url TEXT NOT NULL,
                    published_at TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    transcript TEXT,
                    language TEXT,
                    duration_seconds INTEGER,
                    transcribed_seconds INTEGER,
                    error TEXT,
                    feed_name TEXT,
                    feed_url TEXT,
                    rss_feed_title TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO episodes (id, feed_id, guid, rss_title, audio_url, published_at,
                    status, transcript, language, duration_seconds, transcribed_seconds,
                    error, created_at)
                    SELECT id, feed_id, guid, rss_title, audio_url, published_at,
                    status, transcript, language, duration_seconds, transcribed_seconds,
                    error, created_at FROM _episodes_v1;
                DROP TABLE _episodes_v1;
                CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
                CREATE INDEX IF NOT EXISTS idx_episodes_feed_id ON episodes(feed_id);
                PRAGMA foreign_keys=ON;
            """)
        else:
            for col in ("feed_name", "feed_url", "rss_feed_title"):
                if col not in ep_cols:
                    try:
                        conn.execute(f"ALTER TABLE episodes ADD COLUMN {col} TEXT")
                    except Exception:
                        pass
