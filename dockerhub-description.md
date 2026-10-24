# Podcast Transcriber

Lokalna transkrypcja podcastów dla n8n. Przyjmuje URL pliku audio, transkrybuje
go na CPU (faster-whisper, `large-v3-turbo`) i odsyła gotowy tekst na webhook.

Jeden kontener, jeden proces — kolejka SQLite, API i model w środku. Bez GPU.

```yaml
services:
  podcast:
    image: kpa90/podcast-transcriber:latest
    restart: unless-stopped
    ports:
      - "8130:8080"
    volumes:
      - ./data:/data
    environment:
      - TZ=Europe/Warsaw
      - WEBHOOK_URL=https://twoj-n8n/webhook/xxx
      - MODEL=large-v3-turbo
      - CPU_THREADS=4
    mem_limit: 3g
```

## API

- `POST /api/transcribe` — `{"audio_url": "...", "language": "pl"}` → `202` + `job_id`
- `GET /api/jobs/{id}` — status, postęp, transkrypt
- `GET /` — podgląd: bieżąca transkrypcja z postępem, kolejka, zakończone zadania
- `GET /jobs/{id}` — strona odcinka z transkryptem do skopiowania

Po zakończeniu leci `POST` na `WEBHOOK_URL` ze zdarzeniem `transcription_completed`
i pełnym transkryptem. Nieudana wysyłka nie blokuje kolejki: do 10 prób z odstępem
rosnącym do 1 h, a potem przycisk „Wyślij webhook ponownie” na stronie odcinka.

Każdy transkrypt trafia też do pliku `/data/transcripts/<id> - <tytuł>.txt`.

## Konfiguracja

| Zmienna | Domyślnie |
|---|---|
| `WEBHOOK_URL` | — |
| `MODEL` | `large-v3-turbo` (błędna nazwa = fallback na turbo + ostrzeżenie w UI) |
| `CPU_THREADS` | `4` |
| `IDLE_UNLOAD_SECS` | `300` |
| `MAX_ATTEMPTS` | `3` |
| `MAX_AUDIO_MB` | `1000` |
| `WEBHOOK_MAX_ATTEMPTS` | `10` |

Transkrypcje są sekwencyjne, więc zużycie RAM jest stałe (~1,4 GB w trakcie
pracy, ~100 MB bezczynnie — model jest zwalniany po okresie bezczynności).

Wydajność: ~2,7× realtime na AMD Ryzen 5 7530U, czyli godzinne nagranie w ~21 min.

Kod i pełne benchmarki: https://github.com/kacperpaluch/podcast-transcriber
