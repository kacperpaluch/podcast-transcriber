# Podcast Transcriber

[![Docker Hub](https://img.shields.io/docker/pulls/kpa90/podcast-transcriber?logo=docker&label=Docker%20Hub)](https://hub.docker.com/r/kpa90/podcast-transcriber)

Lokalna transkrypcja podcastów dla n8n. Przyjmuje URL pliku audio, transkrybuje
go na CPU (faster-whisper) i odsyła gotowy tekst na webhook.

Cała aplikacja to **jeden plik, jeden kontener, jeden proces** — kolejka, API
i model w środku. RSS, podsumowania i powiadomienia zostają po stronie n8n.

```
n8n → POST /api/transcribe → kolejka SQLite → whisper → webhook z transkryptem → n8n
```

## Wymagania

- Docker z Compose v2
- x86_64 lub ARM64, **4+ rdzenie**, ~2 GB wolnego RAM na czas transkrypcji
- ~2 GB miejsca na model (pobiera się sam przy pierwszym uruchomieniu)

## Uruchomienie

```bash
git clone git@github.com:kacperpaluch/podcast-transcriber.git
cd podcast-transcriber
WEBHOOK_URL=https://twoj-n8n/webhook/xxx docker compose up -d
```

UI (lista ostatnich zadań) na `http://<host>:8130`.

Dane trzymane są w katalogu zamontowanym jako `/data` — baza `app.db`, pobrane
audio w `audio/` (kasowane po transkrypcji) i cache modelu w `models/`.

## Konfiguracja

Wszystko przez zmienne środowiskowe w `compose.yaml`:

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `WEBHOOK_URL` | — | Adres, na który leci gotowy transkrypt. Pusty = tylko zapis do bazy. |
| `MODEL` | `large-v3-turbo` | Model faster-whisper. |
| `CPU_THREADS` | `4` | Wątki CTranslate2 i limit rdzeni. Zwiększanie **nie przyspiesza** — patrz [BENCHMARKS.md](BENCHMARKS.md). |
| `IDLE_UNLOAD_SECS` | `300` | Po tylu sekundach pustej kolejki model jest zwalniany z RAM. |
| `MAX_ATTEMPTS` | `3` | Po tylu nieudanych próbach zadanie dostaje status `error` zamiast wracać do kolejki. |
| `DB_PATH`, `AUDIO_DIR`, `MODELS_DIR` | `/data/...` | Ścieżki wewnątrz kontenera. |

## API

### `POST /api/transcribe`

Dodaje zadanie do kolejki. Zwraca `202` i `job_id`.

```json
{
  "audio_url": "https://example.com/odcinek.mp3",
  "language": "pl",
  "episode_title": "Tytuł odcinka",
  "feed_name": "Nazwa kanału",
  "rss_feed_title": "Tytuł kanału z RSS",
  "feed_url": "https://example.com/rss.xml",
  "guid": "unikalny-id",
  "published_at": "2026-09-01T10:00:00+00:00"
}
```

Wymagany jest tylko `audio_url`. `language` pominięty = automatyczne wykrycie.
`guid` musi być unikalny — powtórzony zwraca `409`, co daje deduplikację za darmo.

### `GET /api/jobs/{job_id}`

Zwraca wiersz zadania: `status` (`queued`, `running`, `done`, `error`),
`progress_seconds`, `duration_seconds`, `transcript`, `error`.

### `GET /`

Strona HTML z listą 50 ostatnich zadań i ich postępem.

## Webhook

Po zakończeniu transkrypcji aplikacja wysyła `POST` na `WEBHOOK_URL`:

```json
{
  "event": "transcription_completed",
  "feed_name": "...", "rss_feed_title": "...", "feed_url": "...",
  "episode_title": "...", "guid": "...", "audio_url": "...",
  "published_at": "...", "language": "pl", "duration_seconds": 3408,
  "transcript": "pełna treść..."
}
```

Przy niepowodzeniu ponawia 5 razy z narastającym odstępem (5 s → 2 min). Jeśli
wszystkie próby zawiodą, transkrypt zostaje w bazie, a zadanie dostaje adnotację
w polu `error` — nic nie ginie, można odczytać przez `GET /api/jobs/{id}`.

**Kształt tego payloadu to kontrakt z n8n.** Zmiana wymaga zmiany workflow;
`test_app.py` pilnuje, żeby nie stało się to przypadkiem.

## Jak to działa

Jeden wątek roboczy bierze najstarsze zadanie ze statusem `queued`, pobiera
audio, transkrybuje i wysyła webhook. Sekwencyjność nie jest ustawieniem, tylko
konsekwencją jednego wątku — dzięki temu zużycie RAM jest stałe niezależnie od
tego, ile odcinków czeka w kolejce.

Model ładuje się przy pierwszym zadaniu i jest zwalniany po `IDLE_UNLOAD_SECS`
bezczynności. Bezczynna aplikacja zajmuje ~100 MB, w trakcie pracy ~1,4 GB.

Po restarcie zadania ze statusem `running` wracają do kolejki. Licznik `attempts`
chroni przed pętlą: zadanie, które `MAX_ATTEMPTS` razy ubiło proces (np. przez
OOM), ląduje jako `error` zamiast restartować aplikację w nieskończoność.

## Wydajność

Na AMD Ryzen 5 7530U (6C/12T) z modelem `large-v3-turbo`: **~2,7× realtime**,
czyli godzinny odcinek w ~21 minut, przy ~1,4 GB RAM.

Przetestowano osiem konfiguracji i cztery alternatywne silniki (Parakeet TDT,
Qwen3-ASR, whisper `medium`/`small`) — komplet pomiarów wraz z porównaniem
jakości polskich transkrypcji jest w [BENCHMARKS.md](BENCHMARKS.md).
Najkrótsze streszczenie: **nic nie pobiło `large-v3-turbo`**, a dokręcanie
liczby wątków, batchingu czy `beam_size` nie daje nic, bo wąskim gardłem jest
przepustowość pamięci.

## Rozwój

```bash
docker build -t podcast:dev .
docker run --rm -v "$PWD/test_app.py:/app/test_app.py:ro" podcast:dev python test_app.py
```

Publikacja obrazu (multi-arch, `latest` + tag z SHA do rollbacku):

```bash
./build-push.sh
```
