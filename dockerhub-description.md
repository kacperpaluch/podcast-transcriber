# Podcast Transcriber

Lokalny serwis transkrypcji podcastów dla Raspberry Pi 4/5 (8 GB RAM). Przyjmuje URL pliku audio przez REST API, transkrybuje lokalnie (faster-whisper lub Parakeet NVIDIA) i wysyła wyniki webhookiem do n8n. Monitorowanie RSS i orkiestracja po stronie n8n.

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

Zwraca status: `queued`, `transcribing`, `done`, `error` + `progress_pct`.

## Zmienne środowiskowe

| Zmienna | Kontener | Opis |
|---|---|---|
| `DB_PATH` | web, worker | Ścieżka do bazy SQLite (domyślnie `/data/app.db`) |
| `TRANSCRIBER_IMAGE` | worker | Obraz transkrybera (domyślnie `kpa90/podcast-transcriber:latest`) |
| `HOST_DATA_PATH` | worker | Ścieżka do `/data` na hoście (wymagana do montowania wolumenów) |
| `COMPOSE_NETWORK` | worker | Sieć Docker Compose (domyślnie `podcast_default`) |
| `PARAKEET_IMAGE` | worker | Obraz Parakeet (opcjonalnie, dla modelu parakeet-tdt-0.6b-v3) |

## Więcej informacji

Pełna dokumentacja, instrukcja integracji z n8n i opis modeli Whisper/Parakeet:
[codeberg.org/kp90/podcast-transcriber](https://codeberg.org/kp90/podcast-transcriber)
