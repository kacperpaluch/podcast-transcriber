# Uruchomienie na Raspberry Pi przez Portainer

Instrukcja krok po kroku dla początkujących. Zakłada, że masz już zainstalowany Docker i Portainer na Raspberry Pi.

---

## Krok 1 — Utwórz folder na dane

Połącz się z Pi przez SSH i wykonaj jedno polecenie:

```bash
mkdir -p /opt/podcast-transcriber/data/audio /opt/podcast-transcriber/data/models
```

To jest miejsce gdzie aplikacja będzie przechowywać bazę danych, pliki audio i model Whisper (~800 MB, pobierany automatycznie przy pierwszej transkrypcji).

> Możesz wybrać inny folder, np. `/home/pi/podcast-data` — ważne żeby go zapamiętać, przyda się w Kroku 4.

---

## Krok 2 — Otwórz Portainer

W przeglądarce wejdź na adres swojego Portainera, np.:

```
http://192.168.1.100:9000
```

Zaloguj się.

---

## Krok 3 — Pobierz plik konfiguracyjny

Masz dwie opcje:

**Opcja A** — pobierz bezpośrednio na Pi:
```bash
curl -o /tmp/docker-compose.portainer.yml \
  https://codeberg.org/kp90/podcast-transcriber/raw/branch/main/docker-compose.portainer.yml
```

**Opcja B** — skopiuj zawartość pliku `docker-compose.portainer.yml` z tego repozytorium.

---

## Krok 4 — Utwórz Stack w Portainerze

1. W lewym menu kliknij **Stacks**
2. Kliknij przycisk **+ Add stack** (prawy górny róg)
3. W polu **Name** wpisz: `podcast`

### Wklej konfigurację

W sekcji **Build method** wybierz **Web editor**, a następnie wklej zawartość pliku `docker-compose.portainer.yml`.

### Ustaw zmienną środowiskową

Przewiń stronę w dół do sekcji **Environment variables** i kliknij **+ Add an environment variable**:

| Name | Value |
|---|---|
| `HOST_DATA_PATH` | `/opt/podcast-transcriber/data` |

> Jeśli w Kroku 1 wybrałeś inny folder, wpisz jego ścieżkę.

---

## Krok 5 — Uruchom

Kliknij przycisk **Deploy the stack** na dole strony.

Portainer pobierze obrazy z Docker Hub (pierwsze uruchomienie może potrwać kilka minut przy słabym łączu) i uruchomi 3 kontenery:

| Kontener | Status po uruchomieniu |
|---|---|
| `podcast-web` | ✅ Running |
| `podcast-scheduler` | ✅ Running |
| `podcast-worker-controller` | ✅ Running |

---

## Krok 6 — Otwórz interfejs aplikacji

W przeglądarce wejdź na:

```
http://<adres-raspberry-pi>:8080
```

Powinieneś zobaczyć panel główny Podcast Transcriber.

---

## Krok 7 — Konfiguracja

### Dodaj kanał RSS

1. Kliknij **Kanały RSS** w menu po lewej
2. Uzupełnij:
   - **Nazwa kanału** — dowolna, np. `Huberman Lab`
   - **URL RSS** — adres RSS podcastu, np. `https://feeds.megaphone.fm/hubermanlab`
3. Kliknij **Dodaj**

> Przy pierwszym sprawdzeniu scheduler zarejestruje historię kanału bez kolejkowania — tylko nowe odcinki będą transkrybowane automatycznie. Żeby ręcznie dodać konkretny odcinek użyj przycisku **Pobierz ostatni**.

### Ustaw webhook n8n

1. Kliknij **Ustawienia** w menu
2. W polu **URL webhooka n8n** wpisz adres webhooka z n8n
3. Kliknij **Zapisz ustawienia**
4. Kliknij **Wyślij testowego webhooka** — sprawdzi czy n8n odbiera

> W n8n webhook musi być w trybie **Production** (nie Test). Otwórz workflow → kliknij przełącznik w prawym górnym rogu → ustaw na Active.

### Opcjonalnie: zmień model transkrypcji

Domyślnie używany jest `large-v3-turbo` — dobry balans jakości i szybkości. Możesz zmienić na `medium` lub `small` jeśli chcesz szybszych transkrypcji kosztem jakości.

---

## Krok 8 — Przetestuj pełny pipeline

1. W zakładce **Kanały RSS** kliknij **Pobierz ostatni** przy dodanym kanale
2. Przejdź do zakładki **Odcinki** — odcinek powinien mieć status **W kolejce**
3. Przejdź do **Panel główny** — po chwili pojawi się pasek postępu transkrypcji
4. Transkrypcja 30-minutowego podcastu trwa ok. 20–40 minut (zależy od modelu i Pi)
5. Po zakończeniu status zmieni się na **Gotowe** i webhook zostanie wysłany do n8n

---

## Rozwiązywanie problemów

### Kontenery się nie uruchamiają

Sprawdź logi w Portainerze: **Stacks → podcast → (nazwa kontenera) → Logs**

### Transkrypcja nie startuje

Upewnij się że folder z danymi istnieje i ma odpowiednie uprawnienia:
```bash
ls -la /opt/podcast-transcriber/data/
chmod -R 755 /opt/podcast-transcriber/data/
```

### Webhook zwraca 404

W n8n: otwórz workflow z webhookiem → kliknij przełącznik **Inactive/Active** → ustaw na **Active**. Następnie w aplikacji kliknij **Historia webhooków → Wyślij** przy dowolnym odcinku.

### Aktualizacja do nowej wersji

```bash
# W Portainerze: Stacks → podcast → kliknij "Pull and redeploy"
```

Lub przez SSH:
```bash
docker pull kpa90/podcast-web:latest
docker pull kpa90/podcast-scheduler:latest
docker pull kpa90/podcast-worker-controller:latest
docker pull kpa90/podcast-transcriber:latest
# Następnie w Portainerze: Stacks → podcast → Redeploy
```
