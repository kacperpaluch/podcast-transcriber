import sys
sys.path.insert(0, "/app/shared")

import argparse
import logging
import os
import re
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [transcriber] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 30  # zapisuj postęp co tyle sekund audio


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="Path to audio file")
    p.add_argument("--model", default="large-v3-turbo")
    p.add_argument("--compute-type", default="int8", dest="compute_type")
    p.add_argument("--language", default=None)
    return p.parse_args()


def find_episode_id(audio_path: str) -> int | None:
    m = re.search(r"episode_(\d+)", os.path.basename(audio_path))
    return int(m.group(1)) if m else None


def get_audio_duration(audio_path: str) -> int | None:
    try:
        import subprocess, json
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio_path],
            capture_output=True, text=True, timeout=30,
        )
        return int(float(json.loads(r.stdout)["format"]["duration"]))
    except Exception:
        return None


def transcribe_with_progress(audio_path: str, model_name: str, compute_type: str,
                              language: str | None, episode_id: int, duration: int | None):
    from faster_whisper import WhisperModel

    models_dir = "/data/models"
    os.makedirs(models_dir, exist_ok=True)

    log.info("Loading model %s (%s)...", model_name, compute_type)
    model = WhisperModel(model_name, device="cpu", compute_type=compute_type,
                         download_root=models_dir)

    log.info("Starting transcription of %s", audio_path)
    segments, info = model.transcribe(
        audio_path,
        language=language,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5,
    )

    log.info("Detected language: %s (prob=%.2f)", info.language, info.language_probability)

    # Zapisz duration_seconds z info jeśli ffprobe nie dał wyniku
    known_duration = duration or int(info.duration or 0)
    if known_duration:
        with db.db() as conn:
            conn.execute(
                "UPDATE episodes SET duration_seconds=? WHERE id=?",
                (known_duration, episode_id),
            )

    text_parts = []
    last_saved_at = 0.0

    for seg in segments:
        text_parts.append(seg.text.strip())

        # Zapisuj postęp co PROGRESS_INTERVAL sekund audio
        if seg.end - last_saved_at >= PROGRESS_INTERVAL:
            with db.db() as conn:
                conn.execute(
                    "UPDATE episodes SET transcribed_seconds=? WHERE id=?",
                    (int(seg.end), episode_id),
                )
            last_saved_at = seg.end
            if known_duration:
                pct = min(99, int(seg.end / known_duration * 100))
                log.info("Progress: %d%% (%.0f / %ds)", pct, seg.end, known_duration)

    transcript = "\n".join(text_parts)
    return transcript, info.language, known_duration


def main():
    args = parse_args()

    if not os.path.exists(args.input):
        log.error("Audio file not found: %s", args.input)
        sys.exit(1)

    episode_id = find_episode_id(args.input)
    if episode_id is None:
        log.error("Cannot determine episode_id from filename: %s", args.input)
        sys.exit(1)

    log.info("Transcribing episode %d from %s", episode_id, args.input)

    duration = get_audio_duration(args.input)
    if duration:
        log.info("Audio duration: %d seconds (%.1f min)", duration, duration / 60)

    try:
        transcript, language, final_duration = transcribe_with_progress(
            args.input, args.model, args.compute_type, args.language, episode_id, duration
        )
    except Exception as e:
        log.error("Transcription failed: %s", e)
        with db.db() as conn:
            conn.execute(
                "UPDATE episodes SET status='error', error=? WHERE id=?",
                (str(e)[:500], episode_id),
            )
        sys.exit(1)

    with db.db() as conn:
        conn.execute(
            """UPDATE episodes
               SET transcript=?, language=?, duration_seconds=?, transcribed_seconds=?, status='transcribing'
               WHERE id=?""",
            (transcript, language, final_duration, final_duration, episode_id),
        )

    log.info("Done. Episode %d: %d chars, lang=%s, duration=%ds",
             episode_id, len(transcript), language, final_duration)


if __name__ == "__main__":
    main()
