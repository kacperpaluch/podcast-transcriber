# Podcast Transcriber

Lokalny transkryber podcastów dla Raspberry Pi 4/5 (8 GB RAM). Monitoruje kanały RSS, pobiera audio, transkrybuje lokalnie (faster-whisper) i wysyła wyniki webhookiem do n8n.

## Architektura

```
RSS feed → scheduler → kolejka SQLite → worker-controller → transcriber (on-demand) → webhook n8n
```

| Kontener | Rola | RAM |
|---|---|---|
| `podcast-web` | UI + REST API (FastAPI, port 8080) | ~150 MB |
| `podcast-scheduler` | Sprawdza RSS co N minut, kolejkuje nowe odcinki | ~100 MB |
| `podcast-worker-controller` | FIFO queue, uruchamia transkryber sekwencyjnie | ~100 MB |
| `podcast-transcriber` | faster-whisper CPU, uruchamiany on-demand (`--rm`) | ~2–2,5 GB |

**Transkrypcje są zawsze sekwencyjne** — gwarantuje to stabilne ~2,5 GB RAM szczytowo niezależnie od liczby odcinków w kolejce.

## Wymagania

- Raspberry Pi 4 lub 5, **8 GB RAM**, Raspberry Pi OS 64-bit (ARM64)
- Docker + Docker Compose v2
- Dostęp do internetu

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

## Konfiguracja

Przez interfejs webowy:

1. **Kanały RSS** → dodaj kanały (własna nazwa + URL RSS)
2. **Ustawienia** → interwał sprawdzania, model Whisper, URL webhooka n8n
3. **Odcinki** → kolejka, statusy, podgląd transkrypcji
4. **Historia webhooków** → log wysłanych webhooków z możliwością ponownego wysłania

### Modele Whisper

| Model | Jakość | Czas (1h audio) | RAM |
|---|---|---|---|
| `large-v3-turbo` | ★★★★ (domyślny) | ~20–30 min | ~2 GB |
| `large-v3` | ★★★★★ | ~40–60 min | ~2,5 GB |
| `medium` | ★★★ | ~10–15 min | ~1,5 GB |
| `small` | ★★ | ~5–8 min | ~1 GB |

### Parakeet (eksperymentalnie)

Jako alternatywę dla Whispera można wybrać `parakeet-tdt-0.6b-v3` (NVIDIA Parakeet TDT, 25 języków EU
w tym polski). Dekoder TDT jest nieautoregresyjny, więc na CPU bywa szybszy niż Whisper. Kontener
([`ghcr.io/achetronic/parakeet`](https://github.com/achetronic/parakeet)) uruchamiany jest on-demand
przez worker-controller i zamykany po zakończeniu, więc nie zajmuje pamięci między transkrypcjami.

Uwagi dot. Raspberry Pi:

- Eksport ONNX używa pełnej (kwadratowej w długości) atencji, dlatego długie audio jest dzielone na
  fragmenty 2-minutowe (`PARAKEET_CHUNK_SECS`) i transkrybowane po kawałku — bez tego dochodzi do OOM.
- Kontener dostaje limit 4 GB RAM z **wyłączonym swapem** (`--memory-swap=4g`). Dzięki temu przy
  przekroczeniu limitu Docker ubija sam kontener, zamiast wpędzać cały Pi w swap i zawieszać host.
- Każdy kanał ma w UI ustawienie **języka** — Parakeet potrzebuje go jawnie (domyślnie zgadywałby `en`).

## Webhook do n8n

Payload po każdej transkrypcji:

```json
{
  "feed_name": "Nazwa wpisana w UI",
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

## Zachowanie przy pierwszym dodaniu kanału

Przy pierwszym sprawdzeniu RSS scheduler **nie kolejkuje całej historii** — rejestruje ją jako `skipped` (baseline). Kolejkowane są tylko odcinki opublikowane po dodaniu kanału. Aby ręcznie dodać konkretny odcinek, użyj przycisku **Pobierz ostatni** w zakładce Kanały RSS.

## Dane

| Ścieżka | Zawartość |
|---|---|
| `data/app.db` | SQLite: kanały, odcinki, ustawienia, logi webhooków |
| `data/audio/` | Tymczasowe pliki audio (usuwane po wysłaniu webhooka) |
| `data/models/` | Cache modeli Whisper |

## Logi

```bash
docker compose logs -f scheduler
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
