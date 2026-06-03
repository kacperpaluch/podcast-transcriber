import sys
sys.path.insert(0, "/app/shared")

import time
import logging
import os
import subprocess
import json
import httpx
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
# Path used INSIDE this container (for DB, audio reads/writes)
DATA_PATH = "/data"
# Path on the Docker HOST — passed as -v to `docker run podcast-transcriber`
HOST_DATA_PATH = os.environ.get("HOST_DATA_PATH", "/data")
TRANSCRIBER_IMAGE = os.environ.get("TRANSCRIBER_IMAGE", "podcast-transcriber:latest")
TRANSCRIBER_CONTAINER = "podcast-transcriber-active"
PARAKEET_IMAGE = os.environ.get("PARAKEET_IMAGE", "ghcr.io/achetronic/parakeet:latest")
PARAKEET_CONTAINER = "podcast-parakeet-active"
COMPOSE_NETWORK = os.environ.get("COMPOSE_NETWORK", "podcast_default")

POLL_INTERVAL = 10          # seconds between queue checks
WEBHOOK_RETRIES = 5
WEBHOOK_RETRY_DELAYS = [5, 15, 30, 60, 120]


def get_setting(key, default=None):
    with db.db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def is_transcriber_running():
    for container in [TRANSCRIBER_CONTAINER, PARAKEET_CONTAINER]:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format={{.State.Running}}", container],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip() == "true":
                return True
        except Exception:
            pass
    return False


def kill_zombie_transcriber():
    for container in [TRANSCRIBER_CONTAINER, PARAKEET_CONTAINER]:
        try:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10)
        except Exception:
            pass


def download_audio(audio_url: str, episode_id: int) -> str:
    dest = os.path.join(AUDIO_DIR, f"episode_{episode_id}.audio")
    log.info("Downloading audio: %s", audio_url)

    with httpx.stream("GET", audio_url, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)

    log.info("Downloaded to %s (%.1f MB)", dest, os.path.getsize(dest) / 1_048_576)
    return dest


def run_transcriber(audio_path: str, model: str) -> bool:
    # audio_path is /data/audio/episode_X.audio inside this container
    # transcriber sees the same path because we mount HOST_DATA_PATH -> /data
    container_audio_path = "/data" + audio_path[len(DATA_PATH):]

    cmd = [
        "docker", "run", "--rm",
        "--name", TRANSCRIBER_CONTAINER,
        "--memory=4g",
        "-e", "HF_HUB_DISABLE_XET=1",
        "-v", f"{HOST_DATA_PATH}:/data",
        TRANSCRIBER_IMAGE,
        "--input", container_audio_path,
        "--model", model,
        "--compute-type", "int8",
    ]

    log.info("Starting transcriber: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, timeout=7200, capture_output=False)
        if result.returncode != 0:
            log.error("Transcriber exited with code %d", result.returncode)
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("Transcriber timed out — killing container")
        kill_zombie_transcriber()
        return False
    except Exception as e:
        log.error("Transcriber error: %s", e)
        return False


def run_parakeet(audio_path: str, episode_id: int) -> bool:
    try:
        subprocess.run(["docker", "rm", "-f", PARAKEET_CONTAINER], capture_output=True, timeout=10)
    except Exception:
        pass

    cmd = [
        "docker", "run", "-d",
        "--name", PARAKEET_CONTAINER,
        "--memory=3g",
        "--network", COMPOSE_NETWORK,
        PARAKEET_IMAGE,
        "-models", "/models",
        "-workers", "1",
    ]
    log.info("Starting Parakeet: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except Exception as e:
        log.error("Failed to start Parakeet container: %s", e)
        return False

    try:
        log.info("Waiting for Parakeet to be ready...")
        deadline = time.time() + 120
        ready = False
        while time.time() < deadline:
            try:
                r = httpx.get(f"http://{PARAKEET_CONTAINER}:5092/health", timeout=2)
                if r.status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(2)

        if not ready:
            log.error("Parakeet did not become ready in time")
            return False

        log.info("Parakeet ready, sending audio for episode %d", episode_id)
        with open(audio_path, "rb") as f:
            resp = httpx.post(
                f"http://{PARAKEET_CONTAINER}:5092/v1/audio/transcriptions",
                files={"file": ("audio.m4a", f, "audio/mp4")},
                data={"response_format": "json"},
                timeout=7200,
            )
        resp.raise_for_status()
        transcript = resp.json().get("text", "")

        if not transcript:
            log.error("Empty transcript from Parakeet")
            return False

        with db.db() as conn:
            conn.execute(
                "UPDATE episodes SET transcript=?, status='transcribing' WHERE id=?",
                (transcript, episode_id),
            )

        log.info("Parakeet done for episode %d (%d chars)", episode_id, len(transcript))
        return True

    except Exception as e:
        log.error("Parakeet error: %s", e)
        return False
    finally:
        try:
            subprocess.run(["docker", "rm", "-f", PARAKEET_CONTAINER], capture_output=True, timeout=10)
        except Exception:
            pass


def send_webhook(episode_id: int) -> bool:
    webhook_url = get_setting("webhook_url", "")
    if not webhook_url:
        log.warning("No webhook URL configured — skipping")
        return True

    with db.db() as conn:
        row = conn.execute(
            """SELECT e.id, e.guid, e.rss_title, e.audio_url, e.published_at,
                      e.transcript, e.language, e.duration_seconds,
                      f.display_name as feed_name, f.url as feed_url,
                      f.rss_feed_title
               FROM episodes e JOIN feeds f ON f.id = e.feed_id
               WHERE e.id=?""",
            (episode_id,),
        ).fetchone()

    if not row:
        return False

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

    for attempt, delay in enumerate(WEBHOOK_RETRY_DELAYS[:WEBHOOK_RETRIES], 1):
        status_code = None
        error_msg = None
        try:
            resp = httpx.post(webhook_url, json=payload, timeout=30)
            status_code = resp.status_code
            if resp.status_code < 500:
                log.info("Webhook sent (status %d)", resp.status_code)
                _log_webhook(episode_id, status_code=status_code, ok=True)
                return True
            error_msg = f"HTTP {resp.status_code}"
            log.warning("Webhook returned %d, retry %d/%d", resp.status_code, attempt, WEBHOOK_RETRIES)
        except Exception as e:
            error_msg = str(e)[:200]
            log.warning("Webhook error: %s, retry %d/%d", e, attempt, WEBHOOK_RETRIES)
        if attempt < WEBHOOK_RETRIES:
            time.sleep(delay)

    _log_webhook(episode_id, status_code=status_code, ok=False, error=error_msg)
    log.error("Webhook failed after %d attempts for episode %d", WEBHOOK_RETRIES, episode_id)
    return False


def _log_webhook(episode_id: int, *, status_code, ok: bool, error: str | None = None):
    try:
        with db.db() as conn:
            conn.execute(
                "INSERT INTO webhook_log(episode_id, status_code, ok, error) VALUES(?,?,?,?)",
                (episode_id, status_code, 1 if ok else 0, error),
            )
    except Exception as e:
        log.warning("Could not write webhook_log: %s", e)


def process_episode(episode):
    episode_id = episode["id"]
    audio_url = episode["audio_url"]
    model = get_setting("whisper_model", "large-v3-turbo")

    log.info("Processing episode %d: %s", episode_id, episode["rss_title"] or guid_short(episode["guid"]))

    # Mark as transcribing — jeśli odcinek zniknął z DB (np. feed usunięty), pomijamy
    with db.db() as conn:
        affected = conn.execute(
            "UPDATE episodes SET status='transcribing' WHERE id=? AND status='queued'",
            (episode_id,),
        ).rowcount
    if affected == 0:
        log.warning("Episode %d no longer exists or not queued — skipping", episode_id)
        return

    audio_path = None
    try:
        # Download audio
        audio_path = download_audio(audio_url, episode_id)

        # Compute timeout: 2× audio duration or 4 hours max
        # (duration not known yet at download time, so transcriber handles its own timeout)

        # Run transcriber (blocks until done)
        if "parakeet" in model.lower():
            success = run_parakeet(audio_path, episode_id)
        else:
            success = run_transcriber(audio_path, model)

        if not success:
            raise RuntimeError("Transcriber failed")

        # Verify transcript was written
        with db.db() as conn:
            row = conn.execute(
                "SELECT transcript, language, duration_seconds FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()

        if not row or not row["transcript"]:
            raise RuntimeError("Transcript not found in DB after transcriber ran")

        log.info("Transcription complete for episode %d", episode_id)

        # Send webhook
        webhook_ok = send_webhook(episode_id)

        if webhook_ok:
            with db.db() as conn:
                conn.execute(
                    "UPDATE episodes SET status='done' WHERE id=?", (episode_id,)
                )
            # Delete audio only after successful webhook
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
                log.info("Deleted audio: %s", audio_path)
        else:
            # Keep audio, mark error so user can retry
            with db.db() as conn:
                conn.execute(
                    "UPDATE episodes SET status='error', error='Webhook failed after retries' WHERE id=?",
                    (episode_id,),
                )

    except Exception as e:
        log.error("Episode %d failed: %s", episode_id, e)
        with db.db() as conn:
            conn.execute(
                "UPDATE episodes SET status='error', error=? WHERE id=?",
                (str(e)[:500], episode_id),
            )
        # Clean up partial audio download
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass


def guid_short(guid):
    return (guid or "")[:16]


def main():
    db.init_db()
    os.makedirs(AUDIO_DIR, exist_ok=True)
    log.info("Worker controller started")

    # Safety: reset any episodes stuck in 'transcribing' from previous crash
    with db.db() as conn:
        stuck = conn.execute(
            "SELECT id FROM episodes WHERE status='transcribing'"
        ).fetchall()
        for row in stuck:
            log.warning("Resetting stuck episode %d to queued", row["id"])
            conn.execute(
                "UPDATE episodes SET status='queued', error=NULL WHERE id=?", (row["id"],)
            )

    # Kill any zombie transcriber container
    if is_transcriber_running():
        log.warning("Found running transcriber container — killing it")
        kill_zombie_transcriber()

    while True:
        try:
            # Pick exactly ONE queued episode (FIFO)
            with db.db() as conn:
                episode = conn.execute(
                    """SELECT id, feed_id, guid, rss_title, audio_url
                       FROM episodes WHERE status='queued'
                       ORDER BY created_at ASC, id ASC
                       LIMIT 1"""
                ).fetchone()

            if episode:
                process_episode(dict(episode))
            else:
                time.sleep(POLL_INTERVAL)

        except Exception as e:
            log.error("Worker loop error: %s", e)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
