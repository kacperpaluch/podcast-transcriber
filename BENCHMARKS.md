# Benchmarki transkrypcji

Pomiary prędkości transkrypcji na serwerze produkcyjnym. Prowadzone po migracji
z Raspberry Pi na mini-PC, żeby ustalić realny sufit lokalnej transkrypcji.

## Decyzja (19.09.2026)

**Zostajemy przy `large-v3-turbo`, 4 wątki, bez batchingu, `beam_size=5`.**
~2,7× realtime, czyli ~21 minut na godzinę audio.

Przetestowano 8 konfiguracji i 4 alternatywne silniki. Nic nie pobiło turbo
w zestawieniu prędkość/jakość dla polskich podcastów:

| Kandydat | Prędkość | Dlaczego odpadł |
|---|---|---|
| Parakeet TDT | **7,08×** (2,6× szybciej) | przekręca nazwiska gości i nazwy instytucji |
| whisper `small` | 4,79× na fragmencie | jw. — gubi nazwy własne |
| Qwen3-ASR 0.6B | 3,00× | brak interpunkcji, nazwiska gorsze niż Parakeet |
| whisper `medium` | 1,61× | wolniejszy od turbo o 24%, bez zysku jakości |
| Qwen3-ASR 1.7B | < 0,63× | poniżej realtime |

Rozstrzygnął charakter pipeline'u: transkrypcja leci asynchronicznie
(RSS → kolejka → webhook → podsumowanie LLM → Telegram), więc **nikt nie czeka
przed ekranem** i różnica 21 vs 8 minut jest niewidoczna. Za to przekręcone
nazwisko gościa trafia do każdego podsumowania jako zmyślony fakt.

Gdyby kiedyś liczyła się przepustowość (nadrabianie zaległego archiwum),
przełącznikiem jest jedno ustawienie w UI — Parakeet jest gotowy i przetestowany.

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
| whisper, 4 wątki + `beam_size=1` | 2,85× | 2,73 / 2,96 | 398% | 1,33 GB | ➖ +6%, w granicach szumu |
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
3. **`beam_size` nie ma znaczenia dla `large-v3-turbo`.** Zejście z 5 na 1 dało
   +6%, czyli tyle co nic. Turbo ma dekoder czterowarstwowy — beam search mnoży
   pracę dekodera przez pięć, ale dekoder to ułamek kosztu, bo robotę robi enkoder.
   Przy `large-v3` (32 warstwy dekodera) ten test wypadłby inaczej.
4. **Sufit whispera na tej maszynie to ~2,7×, czyli ~21 minut na godzinę audio.**
   Nie podniesie go podmiana biblioteki: `whisper-fastapi`, `docker-whisper`,
   `docker-whisper-live` i `podscripter` mają pod spodem ten sam `faster-whisper`
   + CTranslate2. Podniesie go natomiast zmiana modelu — patrz punkt 4.
5. **Parakeet skaluje się na rdzenie, whisper nie.** Whisper przy 8 wątkach dał
   to samo co przy 4 (555% CPU → 2,67×). Parakeet bierze 949% CPU i osiąga 7,08×,
   czyli realnie korzysta z rdzeni, których whisper nie potrafi wykorzystać.
   Jego dekoder TDT waży 18 MB wobec autoregresyjnego dekodera whispera z
   `beam_size=5`, więc na każdy token czyta z pamięci ułamek tego co whisper.
   Pomiar 7,08× wykonano bez limitu CPU. Dodano `PARAKEET_CPUS` (domyślnie 10),
   bo bez niego kontener zabierał prawie całą maszynę.
6. **GPU to inna liga.** DeepInfra robi ten sam odcinek w sekundy — różnica to
   ~1000× w przepustowości obliczeń plus równoległe dekodowanie wielu okien 30 s.

## Pomiary na fragmencie 120 s

Osobny warsztat do szybkich porównań: pierwsze 120 s tego samego odcinka
(`frag_120s.wav`, 16 kHz mono), transkryber odpalany bezpośrednio przez
`docker run`, z pominięciem kolejki i aplikacji. Test trwa ~1 minutę zamiast 20.

> **Ta metoda jest precyzyjna** — trzy przebiegi turbo dały 2,11 / 2,16 / 2,12×,
> czyli 2% rozrzutu. Szum ±20% z pomiarów pełnego odcinka brał się z próbkowania
> okna 120 s w losowym miejscu nagrania (raz monolog, raz dyskusja z wejściami
> w słowo). Tu audio jest za każdym razem identyczne, więc wynik się powtarza.
>
> **Liczby z fragmentu NIE są porównywalne z pomiarami pełnego odcinka.**
> Kontrola: `large-v3-turbo` daje tu 2,11×, a na pełnym odcinku 2,69×. Powody:
> narzut stały rozkłada się na 120 s zamiast 3408 s, a początek nagrania to
> spokojny monolog wprowadzający (chwilowa prędkość w środku fragmentu sięga
> 3,2×). Wartości z tej tabeli porównuj **wyłącznie między sobą**.
>
> Miara: odstęp `Starting transcription` → `Done.` w logu — wyklucza ładowanie
> modelu, które przy różnych rozmiarach modeli trwa różnie.

| Silnik / model | Prędkość | Próbki | Uwagi |
|---|---|---|---|
| whisper `small` | **4,79×** | 4,79 | 25,1 s — ale gubi nazwy własne, patrz niżej |
| whisper `large-v3-turbo` (kontrola) | **2,13×** | 2,11 / 2,16 / 2,12 | ✅ najlepszy kompromis |
| whisper `medium` | **1,61×** | 1,65 / 1,59 / 1,59 | ❌ wolniejszy od turbo o 24% |
| Parakeet TDT | ~7,08× | — | z pomiaru pełnego odcinka, dla orientacji |
| Qwen3-ASR 0.6B Q8_0 (llama.cpp, 10 wątków) | 3,00× | 3,00 | 40 s, **bez interpunkcji**, nazwiska gorsze niż Parakeet |
| Qwen3-ASR 1.7B Q8_0 (llama.cpp, 10 wątków) | **< 0,63×** | — | przerwany po 3 min 11 s jako bezcelowy |

### Qwen3-ASR — dlaczego odpada

Potwierdziła się hipoteza o dekoderze autoregresyjnym: 0.6B wypadł wolniej od
Parakeeta mimo podobnej liczby parametrów, a 1.7B zszedł poniżej realtime.
Niezależnie od prędkości dyskwalifikuje go jakość wyjścia przez `llama-mtmd-cli`:

- **brak interpunkcji i wielkich liter** — jeden ciągły strumień słów, co dla
  LLM-a robiącego podsumowanie jest gorsze niż błędy fleksyjne Parakeeta
- nazwiska gorsze niż u Parakeeta: „Krzyżownę strachotą" zamiast „Krzysztofem
  Strachotą", zmyślone „Damiel" zamiast „Damian"
- wyjście zawiera preambułę `language Polish<asr_text>`, wymagałoby parsowania

### Cohere Transcribe 2B — jedyny kandydat na poprawę jakości

Nie testowany, ale **nie odrzucony**. Architektonicznie pasuje: duży enkoder
Conformer + lekki dekoder Transformer, czyli ten sam pomysł co `large-v3-turbo`
(przeciwieństwo Qwena). Polski jest jednym z 14 wspieranych języków, interpunkcja
domyślnie włączona, istnieją buildy ONNX int8/q4 (`onnx-community/cohere-transcribe-03-2026-ONNX`).

Przeciw: 2 mld parametrów wobec 809 mln w turbo (~2,5× więcej wag do przepchnięcia
przez pamięć, czyli prawdopodobnie wolniej), model gated na HF, brak gotowego
kontenera z API — trzeba napisać runner na `optimum`/`onnxruntime`.

**Kiedy wrócić:** gdyby whisper zaczął gubić nazwy własne. Dziś nie ma po co —
turbo rozpoznał poprawnie wszystkie nazwiska, nazwę ośrodka i tytuł czasopisma.

### Modele whispera — `medium` jest zdominowany

`medium` wypada **wolniej od `large-v3-turbo` o 24%**, przy porównywalnej jakości.
Powód: ma 24 warstwy dekodera wobec 4 w turbo, a każda z nich atenduje do 1500
stanów enkodera przy każdym generowanym tokenie. Mniejszy enkoder medium nie
odrabia tej straty. **Nie ma powodu, żeby go używać.**

Jakość na tym samym fragmencie (nazwy własne — to one decydują, gdy transkrypt
idzie do LLM-a):

| | `large-v3-turbo` | `medium` | `small` |
|---|---|---|---|
| Zuzanna Krzyżanowska | ✅ | ✅ | ❌ „zuza mną Krzyżanowską" |
| Krzysztof Strachota | ✅ | ✅ | ✅ |
| Mariusz Marszewski | ✅ | ✅ | ✅ |
| czasopismo Kaukaz | ~ „Kaukas" | ✅ | ❌ „Czesopis Makałka z" |
| osw.waw.pl | ❌ „osw.wav.pl" | ✅ | ❌ „www.osw.pl" |
| Ośrodek Studiów Wschodnich | ✅ wielkie litery | ~ małe | ~ małe |

`medium` nie jest jakościowo gorszy od turbo — trafił nawet dwie rzeczy, które
turbo przekręciło. Przegrywa wyłącznie prędkością.

`small` jest **2,3× szybszy od turbo**, ale gubi nazwisko pierwszej gościni
i rozbija tytuł czasopisma. Ta sama wada co u Parakeeta i Qwena: degradacja
uderza w nazwy własne, czyli w to, co ma przetrwać w podsumowaniu.

## Jakość — whisper vs Parakeet

Ten sam fragment (pierwsze ~40 s odcinka), oba silniki, język polski.

| Whisper `large-v3-turbo` | Parakeet TDT |
|---|---|
| Zuzanną Krzyżanowską | **z Uzaną krzyżowską** |
| Krzysztofem Strachotą | Krzysztofem Strachot**u** |
| dr Mariuszem Marszewskim | drem Mariuszem **Marzewskim** |
| czasopisma Kaukaz | czasopisma **kałka z** |
| podcast Ośrodka Studiów Wschodnich | podcast **oświątka studiów wschodniej** |
| „przeszłość, teraźniejszość i przyszłość" | „przeszłość, **teraz mniejszość i przeszłości**" |
| Tekst jest dostępny | **Tst** jest dostępny |

Whisper: 54 144 znaki. Parakeet: 50 327 znaków.

**Błędy Parakeeta kumulują się na nazwach własnych** — czyli dokładnie tam, gdzie
boli, jeśli transkrypt idzie do LLM-a po podsumowanie. Błędy fleksyjne („najciekawszy
elementów") są nieszkodliwe, bo model je przeczyta mimo wszystko; przekręcone
nazwisko gościa albo nazwa instytucji trafi do podsumowania jako zmyślony fakt
albo wypadnie z niego całkiem.

Parakeet gubi też granice zdań — przy 29 szwach co 120 s efekt się kumuluje.

**Wniosek:** w pipelinie asynchronicznym (RSS → kolejka → webhook → podsumowanie)
nikt nie czeka przed ekranem, więc różnica 21 vs 8 minut jest niewidoczna, a
przekręcone nazwiska widać w każdym podsumowaniu. Dlatego domyślnym modelem
zostaje `large-v3-turbo`. Parakeet ma sens przy nadrabianiu zaległego archiwum,
gdzie liczy się przepustowość, a nie precyzja nazwisk.

## Do przetestowania

| Test | Hipoteza | Koszt |
|---|---|---|
| `PARAKEET_CHUNK_SECS` 120 → 300/600 | mniej restartów kontenera (29 → 12 → 6) | stała → zmienna env |
| `medium` / `small` zamiast `large-v3-turbo` | ~1,2–1,5× szybciej, gorsza jakość po polsku | zmiana ustawienia |

`PARAKEET_CHUNK_SECS=120` dobrano pod 8 GB Raspberry Pi. Parakeet liczy pełną
atencję, więc RAM rośnie kwadratowo z długością fragmentu — ale przy 14,5 GB i
limicie 4 GB na kontener jest zapas. Osobny powód cięcia: ONNX Runtime nie zwalnia
aren pamięci między requestami, stąd restart kontenera po każdym chunku.

## Odrzucone bez testu

| Kandydat | Powód |
|---|---|
| `whisper-fastapi`, `docker-whisper`, `docker-whisper-live` | ten sam `faster-whisper` pod spodem — zero zysku prędkości |
| `podscripter` | polski nieobsługiwany (tylko en/es/fr mają optymalizację i testy) |
| diaryzacja (pyannote) | osobny temat, nie wydajnościowy — patrz notatka w rozmowie |

| Qwen3-ASR 0.6B / 1.7B | **przetestowane i odrzucone** — patrz sekcja wyżej |
