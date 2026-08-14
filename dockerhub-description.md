# Podcast Transcriber

Lokalny serwis przygotowania i transkrypcji podcastów dla Raspberry Pi 4/5 (8 GB RAM). Pobiera audio po URL, transkrybuje lokalnie (faster-whisper lub Parakeet NVIDIA) albo przygotowuje małe MP3 dla Groq STT w n8n. Monitorowanie RSS, podsumowania i orkiestracja pozostają po stronie n8n.

## Kontenery

| Obraz | Rola |
|---|---|
| `kpa90/podcast-web` | UI + REST API (FastAPI, port 8080) |
| `kpa90/podcast-worker-controller` | Kolejka FIFO, uruchamia transkryber sekwencyjnie |
| `kpa90/podcast-transcriber` | faster-whisper CPU, uruchamiany on-demand |

## Szybki start (Portainer / docker-compose)

```yaml
name: podcast

services:
  web:
    image: kpa90/podcast-web:latest
    container_name: podcast-web
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - podcast_data:/data
    environment:
      - DB_PATH=/data/app.db
    mem_limit: 300m

  worker-controller:
    image: kpa90/podcast-worker-controller:latest
    container_name: podcast-worker-controller
    restart: unless-stopped
    volumes:
      - podcast_data:/data
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - DB_PATH=/data/app.db
      - TRANSCRIBER_IMAGE=kpa90/podcast-transcriber:latest
      - HOST_DATA_PATH=${HOST_DATA_PATH}
      - COMPOSE_NETWORK=podcast_default
      - PARAKEET_IMAGE=ghcr.io/achetronic/parakeet:latest
      - EXTERNAL_STT_CHUNK_SECS=3600
      - EXTERNAL_STT_AUDIO_BITRATE=32k
    mem_limit: 200m

volumes:
  podcast_data:
```

Ustaw zmienną `HOST_DATA_PATH` na absolutną ścieżkę do katalogu danych na hoście, np. `/opt/podcast-transcriber/data`.

## API

### POST /api/transcribe

```json
{
  "audio_url": "https://example.com/odcinek.mp3",
  "language": "pl",
  "episode_title": "Tytuł odcinka",
  "feed_name": "Nazwa kanału"
}
```

Odpowiedź: `202 Accepted {"job_id": 42}`

### GET /api/jobs/{job_id}

Zwraca status: `queued`, `preparing`, `prepared`, `transcribing`, `done`, `error` + `progress_pct`.

### POST /api/prepare

Kolejkuje przygotowanie plików MP3 dla Groq STT w n8n. Przeznaczony do użycia wyłącznie w prywatnej sieci domowej. Worker koduje audio do mono MP3 16 kHz / 32 kbps i dzieli je domyślnie na części po 60 minut (około 14–15 MB na godzinę); następnie webhook do n8n zawiera manifest chunków.

## Zmienne środowiskowe

| Zmienna | Kontener | Opis |
|---|---|---|
| `DB_PATH` | web, worker | Ścieżka do bazy SQLite (domyślnie `/data/app.db`) |
| `TRANSCRIBER_IMAGE` | worker | Obraz transkrybera (domyślnie `kpa90/podcast-transcriber:latest`) |
| `HOST_DATA_PATH` | worker | Ścieżka do `/data` na hoście (wymagana do montowania wolumenów) |
| `COMPOSE_NETWORK` | worker | Sieć Docker Compose (domyślnie `podcast_default`) |
| `PARAKEET_IMAGE` | worker | Obraz Parakeet (opcjonalnie, dla modelu parakeet-tdt-0.6b-v3) |
| `EXTERNAL_STT_CHUNK_SECS` | worker | Maksymalny czas chunka, domyślnie `3600` s |
| `EXTERNAL_STT_AUDIO_BITRATE` | worker | Bitrate przygotowanego MP3, domyślnie `32k` |

Panel **Odcinki** pozwala trwale usunąć ukończony lub błędny odcinek, aby ten sam GUID można było przetworzyć ponownie przez n8n.

## Więcej informacji

Pełna dokumentacja, instrukcja integracji z n8n i opis modeli Whisper/Parakeet:
[github.com/kacperpaluch/podcast-transcriber](https://github.com/kacperpaluch/podcast-transcriber)
