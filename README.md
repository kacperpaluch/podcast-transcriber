# Podcast Transcriber

[![Docker Hub](https://img.shields.io/docker/pulls/kpa90/podcast-web?logo=docker&label=Docker%20Hub)](https://hub.docker.com/r/kpa90/podcast-web)

Lokalny serwis transkrypcji podcastów dla Raspberry Pi 4/5 (8 GB RAM). Przyjmuje URL pliku audio przez API, transkrybuje lokalnie (faster-whisper lub Parakeet) i wysyła wyniki webhookiem do n8n. Monitorowanie kanałów RSS i orkiestracja obsługiwane są przez n8n.

## Architektura

```
n8n (RSS + logika) → POST /api/transcribe → kolejka SQLite → worker-controller → transcriber → webhook n8n
```

| Kontener | Rola | RAM |
|---|---|---|
| `podcast-web` | UI + REST API (FastAPI, port 8080) | ~150 MB |
| `podcast-worker-controller` | FIFO queue, uruchamia transkryber sekwencyjnie | ~100 MB |
| `podcast-transcriber` | faster-whisper CPU, uruchamiany on-demand (`--rm`) | ~2–2,5 GB |

**Transkrypcje są zawsze sekwencyjne** — gwarantuje to stabilne ~2,5 GB RAM szczytowo niezależnie od liczby odcinków w kolejce.

## Wymagania

- Raspberry Pi 4 lub 5, **8 GB RAM**, Raspberry Pi OS 64-bit (ARM64)
- Docker + Docker Compose v2
- Dostęp do internetu
- n8n do obsługi RSS i dalszego przetwarzania transkrypcji

## Uruchomienie

### 1. Sklonuj repozytorium

```bash
git clone https://codeberg.org/kp90/podcast-transcriber.git
cd podcast-transcriber
```

### 2. Uruchom

```bash
HOST_DATA_PATH=$(pwd)/data docker compose up -d
```

Przy pierwszym uruchomieniu model Whisper (~800 MB) zostanie pobrany automatycznie do `./data/models/` i będzie używany przy kolejnych uruchomieniach.

### 3. Otwórz UI

```
http://<adres-pi>:8080
```

## API

### POST /api/transcribe

Kolejkuje nową transkrypcję. Zwraca `202 Accepted` z `job_id`.

```json
{
  "audio_url": "https://example.com/odcinek.mp3",
  "language": "pl",
  "episode_title": "Tytuł odcinka",
  "feed_name": "Nazwa kanału",
  "rss_feed_title": "Tytuł kanału z RSS",
  "feed_url": "https://example.com/rss.xml",
  "guid": "opcjonalny-unikalny-id",
  "published_at": "2026-06-01T10:00:00+00:00",
  "duration_seconds": 3600
}
```

Odpowiedź: `{"job_id": 42}`

### GET /api/jobs/{job_id}

Zwraca status transkrypcji: `queued`, `transcribing`, `done`, `error` oraz `progress_pct`.

## Konfiguracja

Przez interfejs webowy:

1. **Ustawienia** → model transkrypcji, URL webhooka n8n
2. **Dodaj transkrypcję** → ręczne kolejkowanie (przydatne do testów bez n8n)
3. **Odcinki** → kolejka, statusy, podgląd transkrypcji
4. **Panel główny** → statystyki, aktywna transkrypcja z paskiem postępu
5. **Historia webhooków** → log wysłanych webhooków z możliwością ponownego wysłania

### Modele Whisper

| Model | Jakość | Czas (1h audio) | RAM |
|---|---|---|---|
| `large-v3-turbo` | ★★★★ (domyślny) | ~20–30 min | ~2 GB |
| `large-v3` | ★★★★★ | ~40–60 min | ~2,5 GB |
| `medium` | ★★★ | ~10–15 min | ~1,5 GB |
| `small` | ★★ | ~5–8 min | ~1 GB |

### Parakeet (eksperymentalnie)

Jako alternatywę dla Whispera można wybrać `parakeet-tdt-0.6b-v3` (NVIDIA Parakeet TDT, 25 języków EU w tym polski). Dekoder TDT jest nieautoregresyjny, więc na CPU bywa szybszy niż Whisper. Kontener uruchamiany jest on-demand przez worker-controller i zamykany po zakończeniu.

Uwagi dot. Raspberry Pi:

- Audio dzielone na fragmenty 2-minutowe (`PARAKEET_CHUNK_SECS`) — bez tego dochodzi do OOM
- Kontener dostaje limit 4 GB RAM z **wyłączonym swapem** (`--memory-swap=4g`)
- Język musi być podany jawnie w żądaniu (`language` w POST /api/transcribe)

## Integracja z n8n

### Flow 1 — wykrywanie i zlecanie transkrypcji

```
RSS Feed Trigger → HTTP Request POST /api/transcribe
```

n8n wysyła POST i nie czeka na wynik (fire & forget). Transkrypcja trwa 20–60 minut.

### Flow 2 — odbiór gotowej transkrypcji

```
Webhook Trigger (stały URL) → odbiera transkrypcję → przetwarza dalej
```

Ustaw ten URL jako **URL webhooka** w Ustawieniach aplikacji. Po każdej transkrypcji aplikacja automatycznie wysyła wynik na ten adres.

## Webhook payload

```json
{
  "feed_name": "Nazwa kanału",
  "rss_feed_title": "Tytuł kanału z RSS",
  "feed_url": "https://.../rss.xml",
  "episode_title": "Tytuł odcinka",
  "guid": "unikalny-id",
  "audio_url": "https://.../odcinek.mp3",
  "published_at": "2026-06-01T10:00:00+00:00",
  "language": "pl",
  "transcript": "Pełna transkrypcja...",
  "duration_seconds": 3600
}
```

## Portainer / wdrożenie bez budowania

Użyj pliku `docker-compose.portainer.yml` — korzysta z gotowych obrazów z Docker Hub, bez potrzeby budowania lokalnie. Ustaw zmienną `HOST_DATA_PATH` na absolutną ścieżkę do katalogu danych na hoście.

## Dane

| Ścieżka | Zawartość |
|---|---|
| `data/app.db` | SQLite: odcinki, ustawienia, logi webhooków |
| `data/audio/` | Tymczasowe pliki audio (usuwane po wysłaniu webhooka) |
| `data/models/` | Cache modeli Whisper |

## Logi

```bash
docker compose logs -f worker-controller
docker compose logs -f web
```

## Bezpieczeństwo

`worker-controller` wymaga dostępu do Docker socket (`/var/run/docker.sock`) — daje to efektywnie uprawnienia root na hoście. Akceptowalne na prywatnym Raspberry Pi, nie wystawiaj portu 8080 publicznie bez uwierzytelnienia.

## Build i push na Docker Hub

```bash
./build-push.sh <login-dockerhub>          # build + push :latest
./build-push.sh <login-dockerhub> 1.0.0    # build + push z tagiem wersji
PUSH=0 ./build-push.sh <login-dockerhub>   # tylko build lokalny
```
