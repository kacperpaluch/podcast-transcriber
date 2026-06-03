import sys
sys.path.insert(0, "/app/shared")

import time
import logging
import os
import hashlib
import feedparser
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [scheduler] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def fetch_and_queue():
    with db.db() as conn:
        interval_str = get_setting(conn, "check_interval_minutes", "30")
        feeds = conn.execute(
            "SELECT id, display_name, url, baseline_established FROM feeds WHERE enabled=1"
        ).fetchall()

    for feed in feeds:
        try:
            process_feed(feed)
        except Exception as e:
            log.error("Error processing feed %s: %s", feed["url"], e)

    return int(interval_str)


def process_feed(feed):
    log.info("Checking feed: %s (%s)", feed["display_name"], feed["url"])
    parsed = feedparser.parse(feed["url"])

    rss_feed_title = getattr(parsed.feed, "title", None)

    with db.db() as conn:
        conn.execute(
            "UPDATE feeds SET rss_feed_title=? WHERE id=?",
            (rss_feed_title, feed["id"]),
        )

    is_first_run = not feed["baseline_established"]

    if is_first_run:
        _establish_baseline(feed, parsed)
        return

    # Normalne sprawdzanie — kolejkuj tylko odcinki, których nie ma jeszcze w bazie
    new_count = 0
    for entry in parsed.entries:
        guid = _get_guid(entry)
        audio_url = _find_audio_url(entry)
        if not audio_url:
            continue

        with db.db() as conn:
            existing = conn.execute(
                "SELECT id FROM episodes WHERE guid=?", (guid,)
            ).fetchone()
            if existing:
                continue

            conn.execute(
                """INSERT INTO episodes(feed_id, guid, rss_title, audio_url, published_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (feed["id"], guid, getattr(entry, "title", None),
                 audio_url, _parse_date(entry)),
            )
            new_count += 1
            log.info("Queued new episode: %s", getattr(entry, "title", guid[:16]))

    if new_count:
        log.info("Added %d new episode(s) from %s", new_count, feed["display_name"])


def _establish_baseline(feed, parsed):
    """
    Pierwsze sprawdzenie feedu: rejestruje wszystkie istniejące odcinki
    jako 'skipped' (widziane, ale nie do transkrypcji). Dzięki temu
    kolejne sprawdzenia kolejkują tylko naprawdę nowe odcinki.
    """
    count = 0
    for entry in parsed.entries:
        guid = _get_guid(entry)
        audio_url = _find_audio_url(entry)
        if not audio_url:
            continue

        with db.db() as conn:
            existing = conn.execute(
                "SELECT id FROM episodes WHERE guid=?", (guid,)
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO episodes(feed_id, guid, rss_title, audio_url, published_at, status)
                   VALUES(?, ?, ?, ?, ?, 'skipped')""",
                (feed["id"], guid, getattr(entry, "title", None),
                 audio_url, _parse_date(entry)),
            )
            count += 1

    with db.db() as conn:
        conn.execute(
            "UPDATE feeds SET baseline_established=1 WHERE id=?", (feed["id"],)
        )

    log.info(
        "Baseline established for '%s': %d historical episodes marked as skipped",
        feed["display_name"], count,
    )


def _get_guid(entry):
    guid = getattr(entry, "id", None) or getattr(entry, "link", None)
    if not guid:
        raw = f"{getattr(entry, 'title', '')}{getattr(entry, 'published', '')}"
        guid = hashlib.sha1(raw.encode()).hexdigest()
    return guid


def _find_audio_url(entry):
    for enc in getattr(entry, "enclosures", []):
        ctype = getattr(enc, "type", "")
        url = getattr(enc, "href", "") or getattr(enc, "url", "")
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


def main():
    db.init_db()
    log.info("Scheduler started")
    while True:
        try:
            interval = fetch_and_queue()
        except Exception as e:
            log.error("Scheduler error: %s", e)
            interval = 30
        log.info("Sleeping %d minutes until next check", interval)
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
