import sys
sys.path.insert(0, "/app/shared")

import time
import logging
import os
import subprocess
import json
import glob
import shutil
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
PARAKEET_CHUNK_SECS = 120  # 2 minutes — Parakeet ONNX uses full attention (quadratic RAM)
EXTERNAL_STT_CHUNK_SECS = int(os.environ.get("EXTERNAL_STT_CHUNK_SECS", "1800"))
EXTERNAL_STT_AUDIO_BITRATE = os.environ.get("EXTERNAL_STT_AUDIO_BITRATE", "32k")

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


def _audio_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _split_audio(path: str, chunk_secs: int) -> list:
    base = path.rsplit(".", 1)[0]
    pattern = f"{base}_part_%03d.wav"
    r = subprocess.run([
        "ffmpeg", "-y", "-i", path,
        "-ar", "16000", "-ac", "1",
        "-f", "segment", "-segment_time", str(chunk_secs),
        "-reset_timestamps", "1",
        pattern,
    ], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        log.warning("ffmpeg split failed (code %d): %s", r.returncode, r.stderr[-300:])
        return [path]
    chunks = sorted(glob.glob(f"{base}_part_*.wav"))
    return chunks if chunks else [path]


def prepare_external_stt_audio(audio_path: str, episode_id: int) -> list:
    """Create small speech-optimised MP3 chunks for an external STT provider."""
    duration = _audio_duration(audio_path)
    output_dir = os.path.join(AUDIO_DIR, f"episode_{episode_id}_external_stt")
    shutil.rmtree(output_dir, ignore_errors=True)
    os.makedirs(output_dir, exist_ok=True)
    pattern = os.path.join(output_dir, "chunk_%03d.mp3")
    command = [
        "ffmpeg", "-nostdin", "-y", "-i", audio_path,
        "-map", "0:a:0", "-vn", "-ar", "16000", "-ac", "1",
        "-c:a", "libmp3lame", "-b:a", EXTERNAL_STT_AUDIO_BITRATE,
        "-f", "segment", "-segment_time", str(EXTERNAL_STT_CHUNK_SECS),
        "-reset_timestamps", "1", pattern,
    ]
    log.info("Preparing external-STT audio with %ds chunks at %s", EXTERNAL_STT_CHUNK_SECS, EXTERNAL_STT_AUDIO_BITRATE)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise RuntimeError("FFmpeg preparation timed out")
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg preparation failed: {result.stderr[-500:]}")
    paths = sorted(glob.glob(os.path.join(output_dir, "chunk_*.mp3")))
    if not paths:
        raise RuntimeError("FFmpeg did not produce any external-STT chunks")
    manifest = []
    for index, chunk_path in enumerate(paths):
        start_seconds = index * EXTERNAL_STT_CHUNK_SECS
        manifest.append({
            "index": index,
            "file_name": os.path.basename(chunk_path),
            "size_bytes": os.path.getsize(chunk_path),
            "start_seconds": start_seconds,
            "duration_seconds": max(0, min(EXTERNAL_STT_CHUNK_SECS, int(duration) - start_seconds)) if duration else None,
        })
    return manifest


def _stop_parakeet() -> None:
    try:
        subprocess.run(["docker", "rm", "-f", PARAKEET_CONTAINER], capture_output=True, timeout=15)
    except Exception:
        pass


def _start_parakeet() -> bool:
    _stop_parakeet()
    cmd = [
        "docker", "run", "-d",
        "--name", PARAKEET_CONTAINER,
        "--memory=4g",
        "--memory-swap=4g",
        "--network", "container:podcast-worker-controller",
        PARAKEET_IMAGE,
        "-models", "/models",
        "-workers", "1",
        "-ffmpeg-timeout", "10m",
        "-log-level", "debug",
    ]
    log.info("Starting Parakeet: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log.error("Failed to start Parakeet container (code %d): %s", result.returncode, result.stderr.strip())
            return False
    except Exception as e:
        log.error("Failed to start Parakeet container: %s", e)
        return False

    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            r = httpx.get("http://localhost:5092/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    log.error("Parakeet did not become ready in time")
    return False


def _log_parakeet_crash() -> None:
    try:
        exit_code = subprocess.run(
            ["docker", "inspect", "--format={{.State.ExitCode}}", PARAKEET_CONTAINER],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        logs = subprocess.run(
            ["docker", "logs", "--tail", "50", PARAKEET_CONTAINER],
            capture_output=True, text=True, timeout=5,
        )
        output = (logs.stdout + logs.stderr).strip()
        log.error("Parakeet exit code: %s\nContainer logs:\n%s", exit_code, output)
    except Exception:
        pass


def run_parakeet(audio_path: str, episode_id: int, language: str | None = None) -> bool:
    duration = _audio_duration(audio_path)
    if duration > 0:
        with db.db() as conn:
            conn.execute(
                "UPDATE episodes SET duration_seconds=?, transcribed_seconds=0 WHERE id=?",
                (int(duration), episode_id),
            )
    if duration > PARAKEET_CHUNK_SECS:
        log.info("Audio %.0fs > %ds, splitting into chunks", duration, PARAKEET_CHUNK_SECS)
        chunks = _split_audio(audio_path, PARAKEET_CHUNK_SECS)
        log.info("Split into %d chunks", len(chunks))
    else:
        chunks = [audio_path]

    try:
        parts = []
        for i, chunk in enumerate(chunks):
            log.info("Transcribing chunk %d/%d", i + 1, len(chunks))
            # Restart Parakeet per chunk: the ONNX Runtime memory arenas grow
            # across requests and OOM the container after a few chunks. A fresh
            # container resets memory; model load is fast (~0.1s).
            if not _start_parakeet():
                return False

            form = {"response_format": "json"}
            if language:
                form["language"] = language
            is_wav = chunk.endswith(".wav")
            fname = "audio.wav" if is_wav else "audio.m4a"
            ctype = "audio/wav" if is_wav else "audio/mp4"
            try:
                with open(chunk, "rb") as f:
                    resp = httpx.post(
                        "http://localhost:5092/v1/audio/transcriptions",
                        files={"file": (fname, f, ctype)},
                        data=form,
                        timeout=7200,
                    )
                resp.raise_for_status()
            except Exception as e:
                log.error("Parakeet error on chunk %d/%d: %s", i + 1, len(chunks), e)
                _log_parakeet_crash()
                return False

            parts.append(resp.json().get("text", ""))
            if chunk != audio_path:
                try:
                    os.remove(chunk)
                except Exception:
                    pass
            if duration > 0:
                processed = min(int((i + 1) * PARAKEET_CHUNK_SECS), int(duration))
                with db.db() as conn:
                    conn.execute(
                        "UPDATE episodes SET transcribed_seconds=? WHERE id=?",
                        (processed, episode_id),
                    )

        transcript = "\n".join(parts).strip()
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
    finally:
        _stop_parakeet()


def send_webhook(episode_id: int) -> bool:
    webhook_url = get_setting("webhook_url", "")
    if not webhook_url:
        log.warning("No webhook URL configured — skipping")
        return True

    with db.db() as conn:
        row = conn.execute(
            """SELECT e.id, e.guid, e.rss_title, e.audio_url, e.published_at,
                      e.transcript, e.language, e.duration_seconds, e.processing_mode,
                      e.prepared_manifest,
                      COALESCE(f.display_name, e.feed_name) as feed_name,
                      COALESCE(f.url, e.feed_url) as feed_url,
                      COALESCE(f.rss_feed_title, e.rss_feed_title) as rss_feed_title
               FROM episodes e LEFT JOIN feeds f ON f.id = e.feed_id
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
        "duration_seconds": row["duration_seconds"],
    }
    if row["processing_mode"] == "prepare":
        try:
            chunks = json.loads(row["prepared_manifest"] or "[]")
        except json.JSONDecodeError:
            log.error("Prepared manifest is invalid for episode %d", episode_id)
            return False
        payload.update({"event": "audio_prepared", "job_id": episode_id, "chunks": chunks})
    else:
        payload.update({"event": "transcription_completed", "transcript": row["transcript"]})

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
    processing_mode = episode.get("processing_mode", "transcribe")
    # Language: use episode's own field; fall back to feed language for legacy RSS episodes
    feed_language = episode.get("language")
    if not feed_language and episode.get("feed_id"):
        with db.db() as conn:
            feed_row = conn.execute("SELECT language FROM feeds WHERE id=?", (episode["feed_id"],)).fetchone()
        feed_language = feed_row["language"] if feed_row else None

    log.info("Processing episode %d: %s", episode_id, episode["rss_title"] or guid_short(episode["guid"]))

    in_progress_status = "preparing" if processing_mode == "prepare" else "transcribing"
    # Mark as in progress — jeśli odcinek zniknął z DB (np. feed usunięty), pomijamy
    with db.db() as conn:
        affected = conn.execute(
            "UPDATE episodes SET status=? WHERE id=? AND status='queued'",
            (in_progress_status, episode_id),
        ).rowcount
    if affected == 0:
        log.warning("Episode %d no longer exists or not queued — skipping", episode_id)
        return

    audio_path = None
    try:
        # Download audio
        audio_path = download_audio(audio_url, episode_id)

        if processing_mode == "prepare":
            manifest = prepare_external_stt_audio(audio_path, episode_id)
            with db.db() as conn:
                conn.execute(
                    """UPDATE episodes
                       SET prepared_manifest=?, status='prepared', duration_seconds=?, transcribed_seconds=0
                       WHERE id=?""",
                    (json.dumps(manifest), int(_audio_duration(audio_path)) or None, episode_id),
                )
            if not send_webhook(episode_id):
                raise RuntimeError("Prepared-audio webhook failed after retries")
            if os.path.exists(audio_path):
                os.remove(audio_path)
            log.info("External-STT audio prepared for episode %d (%d chunks)", episode_id, len(manifest))
            return

        # Run transcriber (blocks until done)
        if "parakeet" in model.lower():
            success = run_parakeet(audio_path, episode_id, language=feed_language)
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
            "SELECT id FROM episodes WHERE status IN ('transcribing', 'preparing')"
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
                    """SELECT id, feed_id, guid, rss_title, audio_url, language, processing_mode
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
