# Benchmarki transkrypcji

Pomiary prędkości transkrypcji na serwerze produkcyjnym. Prowadzone po migracji
z Raspberry Pi na mini-PC, żeby ustalić realny sufit lokalnej transkrypcji.

## Sprzęt

| | stary | nowy |
|---|---|---|
| Maszyna | Raspberry Pi 5 | Geekom (`geekom`) |
| CPU | ARM Cortex-A76, 4 rdzenie | AMD Ryzen 5 7530U, 6C/12T, do 4,55 GHz |
| Instrukcje | NEON | AVX2, FMA (brak AVX-512) |
| RAM | 8 GB | 14,5 GB |
| GPU | brak | Radeon iGPU (bez CUDA — nieużywane) |
| OS | Raspberry Pi OS arm64 | Ubuntu 26.04, Docker 29.8 |

Serwer nie jest dedykowany — chodzi na nim ~41 innych kontenerów (n8n, Immich,
Meilisearch), load 3–6. To ma znaczenie dla interpretacji wyników, patrz niżej.

## Metoda

- Materiał: ten sam odcinek za każdym razem — 3409 s (56:49), 130 MB MP3, polski.
- Miara: **× realtime** = sekundy przetworzonego audio na sekundę zegara.
  `2,0×` oznacza, że godzinne nagranie zajmuje 30 minut.
- Odczyt: przyrost `transcribed_seconds` w bazie w oknie 90–120 s, **po**
  załadowaniu modelu (pierwsze pomiary bez rozgrzewki były zafałszowane).
- Dla Parakeeta: odstępy między wpisami `Transcribing chunk i/29` w logu —
  postęp aktualizuje się skokowo co 120 s audio, więc okno czasowe dałoby
  kwantyzowane śmieci.

> **Szum pomiarowy.** Przy identycznej konfiguracji zdarzył się rozrzut
> 1,68–2,83×. Na tym hoście **różnice poniżej ~20% są niemierzalne** — traktuj
> pojedynczą próbkę jako orientacyjną, a wnioski wyciągaj z co najmniej dwóch.

## Wyniki

Wszystkie pomiary: 19.09.2026, model domyślny `large-v3-turbo`, `compute_type=int8`.

| Konfiguracja | Prędkość | Próbki | CPU | RAM | Werdykt |
|---|---|---|---|---|---|
| **whisper, 4 wątki** (domyślne CTranslate2) | **2,69×** | 2,69 | 383% | 1,35 GB | ✅ **optimum** |
| whisper, 8 wątków | 2,67× | 2,86 / 2,48 | 555% | 1,47 GB | ❌ bez zysku, +45% CPU |
| whisper, 4 wątki + `batch_size=8` | 2,26× | 1,68 / 2,83 | 362% | 2,00 GB | ❌ bez zysku, +0,6 GB RAM |
| **parakeet-tdt-0.6b-v3**, chunki 120 s | **7,08×** | 19 chunków, rozrzut 16,7–17,1 s | 949% | 1,71 GB | ✅ **2,6× szybszy od whispera** |

Dla odniesienia: Raspberry Pi 5 robiło ten sam odcinek w ~1,5–2 h (≈0,5×).

Czasy dla odcinka 57 min: whisper **21 min**, Parakeet **8 min 0 s** (29 chunków
po ~16,9 s, plus pobranie i cięcie ~7 s). Transkrypt: 50 327 znaków, polski,
z interpunkcją i wielkimi literami.

### Wnioski

1. **Dla whispera wąskim gardłem jest przepustowość pamięci, nie liczba rdzeni.** Int8 w kółko
   przelatuje przez wagi modelu; dołożenie wątków powoduje, że rdzenie czekają na
   RAM zamiast liczyć. Stąd `WHISPER_CPU_THREADS=4` jako ustawienie produkcyjne —
   ta sama prędkość co przy 8, a pozostałe kontenery odzyskują ~1,5 rdzenia.
2. **Batching (`BatchedInferencePipeline`) opłaca się na GPU, nie na CPU.**
   Grupowanie okien po VAD zwiększa zbiór roboczy bez zwiększania przepustowości.
3. **Sufit whispera na tej maszynie to ~2,7×, czyli ~21 minut na godzinę audio.**
   Nie podniesie go podmiana biblioteki: `whisper-fastapi`, `docker-whisper`,
   `docker-whisper-live` i `podscripter` mają pod spodem ten sam `faster-whisper`
   + CTranslate2. Podniesie go natomiast zmiana modelu — patrz punkt 4.
4. **Parakeet skaluje się na rdzenie, whisper nie.** Whisper przy 8 wątkach dał
   to samo co przy 4 (555% CPU → 2,67×). Parakeet bierze 949% CPU i osiąga 7,08×,
   czyli realnie korzysta z rdzeni, których whisper nie potrafi wykorzystać.
   Jego dekoder TDT waży 18 MB wobec autoregresyjnego dekodera whispera z
   `beam_size=5`, więc na każdy token czyta z pamięci ułamek tego co whisper.
   Pomiar 7,08× wykonano bez limitu CPU. Dodano `PARAKEET_CPUS` (domyślnie 10),
   bo bez niego kontener zabierał prawie całą maszynę.
5. **GPU to inna liga.** DeepInfra robi ten sam odcinek w sekundy — różnica to
   ~1000× w przepustowości obliczeń plus równoległe dekodowanie wielu okien 30 s.

## Do przetestowania

| Test | Hipoteza | Koszt |
|---|---|---|
| `beam_size=5` → `1` | 1,3–1,5× szybciej, minimalnie gorsza jakość | 1 linia |
| `PARAKEET_CHUNK_SECS` 120 → 300/600 | mniej restartów kontenera (29 → 12 → 6) | stała → zmienna env |
| Qwen3-ASR 0.6B / 1.7B przez llama.cpp (GGUF Q8_0) | prawdopodobnie wolniej niż Parakeet — dekoder autoregresyjny czyta wagi dekodera na każdy token (~1,8 GB dla 1.7B Q8_0 vs 18 MB dekodera TDT) | osobny kontener llama.cpp |

`PARAKEET_CHUNK_SECS=120` dobrano pod 8 GB Raspberry Pi. Parakeet liczy pełną
atencję, więc RAM rośnie kwadratowo z długością fragmentu — ale przy 14,5 GB i
limicie 4 GB na kontener jest zapas. Osobny powód cięcia: ONNX Runtime nie zwalnia
aren pamięci między requestami, stąd restart kontenera po każdym chunku.

## Odrzucone bez testu

| Kandydat | Powód |
|---|---|
| `whisper-fastapi`, `docker-whisper`, `docker-whisper-live` | ten sam `faster-whisper` pod spodem — zero zysku prędkości |
| `podscripter` | polski nieobsługiwany (tylko en/es/fr mają optymalizację i testy) |
| Cohere Transcribe 2B | dekoder autoregresyjny, brak kwantyzowanego buildu na CPU |
| diaryzacja (pyannote) | osobny temat, nie wydajnościowy — patrz notatka w rozmowie |

Qwen3-ASR ma oficjalne buildy GGUF (`ggml-org/Qwen3-ASR-{0.6B,1.7B}-GGUF`, Q8_0),
więc lokalny test przez `llama.cpp` jest wykonalny — patrz „Do przetestowania".
Cohere na razie bez kwantyzacji pod CPU; ma sens przez API na GPU.
