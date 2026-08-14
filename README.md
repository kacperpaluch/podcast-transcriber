# Podcast Transcriber

[![Docker Hub](https://img.shields.io/docker/pulls/kpa90/podcast-web?logo=docker&label=Docker%20Hub)](https://hub.docker.com/r/kpa90/podcast-web)

Lokalny serwis przygotowania i transkrypcji podcastów dla Raspberry Pi 4/5 (8 GB RAM). Przyjmuje URL pliku audio przez API; może transkrybować go lokalnie (faster-whisper lub Parakeet) albo przygotować małe pliki MP3 dla zewnętrznego STT. Monitorowanie RSS, wybór dostawcy STT, podsumowanie i Telegram pozostają w n8n.

## Architektura

```
n8n (RSS + logika) → POST /api/transcribe → kolejka SQLite → worker-controller → transcriber → webhook n8n

n8n (RSS + zewnętrzny STT) → POST /api/prepare → kolejka SQLite → FFmpeg → webhook z manifestem → wybrany STT w n8n
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

Zwraca status transkrypcji lub przygotowania audio: `queued`, `preparing`, `prepared`, `transcribing`, `done`, `error` oraz `progress_pct`.

### POST /api/prepare

Kolejkuje pobranie i przygotowanie audio do zewnętrznego STT. Endpoint jest przeznaczony do użycia wyłącznie w prywatnej sieci domowej.

Żądanie ma ten sam format co `/api/transcribe`. Worker pobiera audio bezpośrednio z URL, koduje je do MP3 mono 16 kHz / 32 kbps i dzieli domyślnie na części po 60 minut (około 14–15 MB na godzinę, poniżej limitu 25 MB). Po przygotowaniu wysyła skonfigurowany webhook do n8n z `event: "audio_prepared"`, `job_id` i manifestem chunków.

### GET /api/jobs/{job_id}/chunks/{chunk_index}

Pobiera pojedynczy przygotowany MP3 dla n8n w prywatnej sieci domowej.

### POST /api/jobs/{job_id}/cleanup

Usuwa przygotowane pliki po pomyślnej transkrypcji i podsumowaniu w n8n.

### DELETE /api/episodes/{episode_id}

Trwale usuwa zakończony, błędny lub przygotowany do zewnętrznego STT odcinek oraz jego pliki robocze. Dzięki temu ten sam GUID można ponownie uruchomić z RSS przez nową ścieżkę zewnętrznego STT. Endpoint nie pozwala usuwać zadań `queued`, `preparing` ani `transcribing`.

W tabeli odcinków na **Panelu głównym** odpowiada mu przycisk **Usuń** z potwierdzeniem; działa też dla pozycji `prepared`, usuwając jej chunki i odblokowując GUID. Przycisk **Usuń całą historię** usuwa przygotowane do zewnętrznego STT, zakończone, błędne i pominięte odcinki; zadania `queued`, `preparing` i `transcribing` pozostają nienaruszone. Przycisk **Transkrybuj ponownie** zachowuje istniejący tryb zadania; aby przełączyć historyczny odcinek na nową ścieżkę, najpierw wybierz **Usuń**, a następnie uruchom go ponownie w n8n.

## Konfiguracja

Przez interfejs webowy:

1. **Ustawienia** → model transkrypcji, URL webhooka n8n
2. **Dodaj transkrypcję** → wybór: lokalna transkrypcja albo **FFmpeg → chunki → webhook n8n/zewnętrzny STT** (zalecany)
3. **Panel główny** → statystyki, aktywna transkrypcja, pełna lista odcinków sortowana chronologicznie, filtrowanie i bezpieczne czyszczenie historii
4. **Historia webhooków** → log wysłanych webhooków z możliwością ponownego wysłania

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

### Flow 1 — lokalna transkrypcja (dotychczasowy)

```
RSS Feed Trigger → HTTP Request POST /api/transcribe
```

n8n wysyła POST i nie czeka na wynik (fire & forget). Transkrypcja trwa 20–60 minut.

### Flow 2 — odbiór gotowej transkrypcji

```
Webhook Trigger (stały URL) → odbiera transkrypcję → przetwarza dalej
```

Ustaw ten URL jako **URL webhooka** w Ustawieniach aplikacji. Po każdej transkrypcji aplikacja automatycznie wysyła wynik na ten adres.

### Flow 3 — zewnętrzny STT w n8n (rekomendowany)

```
RSS Feed Trigger → HTTP Request POST /api/prepare → webhook audio_prepared
→ pobierz każdy chunk → wybrany endpoint STT → scal tekst
→ podsumowanie → Telegram → POST /api/jobs/{job_id}/cleanup
```

Trzy żądania serwisu (`/api/prepare`, pobieranie chunków, `/cleanup`) nie wymagają credentialu; są przeznaczone dla prywatnej sieci domowej. Klucz do wybranego dostawcy STT pozostaje w credentialu skonfigurowanym przez użytkownika w n8n.

## Webhook payload

```json
{
  "event": "audio_prepared",
  "job_id": 42,
  "feed_name": "Nazwa kanału",
  "rss_feed_title": "Tytuł kanału z RSS",
  "feed_url": "https://.../rss.xml",
  "episode_title": "Tytuł odcinka",
  "guid": "unikalny-id",
  "audio_url": "https://.../odcinek.mp3",
  "published_at": "2026-06-01T10:00:00+00:00",
  "language": "pl",
  "duration_seconds": 3600,
  "chunks": [
    {"index": 0, "file_name": "chunk_000.mp3", "size_bytes": 14400000, "start_seconds": 0, "duration_seconds": 3600}
  ]
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

Endpointy przygotowania audio nie mają uwierzytelnienia; nie wystawiaj portu 8033 poza prywatną sieć domową.

## Build i push na Docker Hub

```bash
./build-push.sh <login-dockerhub>          # build + push :latest + :<git-sha>
./build-push.sh <login-dockerhub> 1.0.0    # build + push z tagiem wersji + :<git-sha>
PUSH=0 ./build-push.sh <login-dockerhub>   # tylko build lokalny
```

Każdy build taguje obraz dwoma tagami: `:latest` (ruchomy) + `:<git-sha>` (stały backup do rollbacku).

## Historia zmian

- **2026-06-19** — porządki: usunięto nieużywaną zależność `python-multipart` z web, martwy wewnętrzny import `json` w worker-controller, komentarz opisujący niezaimplementowany timeout. Brak zmian w API.
