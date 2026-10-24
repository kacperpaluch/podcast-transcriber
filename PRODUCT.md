# Product

<!-- impeccable:product-schema 1 -->

Zapisane bez wywiadu: właściciel kazał decydować samodzielnie. Fakty oznaczone
„(założenie)" wynikają z kodu i README, nie z rozmowy.

## Platform

web

## Users

Jeden właściciel homelaba, który podpiął aplikację do n8n. Na stronę zagląda
od czasu do czasu, z laptopa albo telefonu, żeby sprawdzić, czy odcinek już się
przetworzył, co stoi w kolejce i czy coś się nie wysypało (założenie).

## Product Purpose

Lokalna transkrypcja podcastów na CPU. n8n wysyła URL audio, aplikacja
transkrybuje i odsyła tekst webhookiem. UI służy tylko do podglądu: co się dzieje
teraz, co czeka, co się udało, co nie i dlaczego, oraz do przeczytania i
skopiowania gotowego transkryptu.

## Operating Context

Jeden proces, jeden wątek roboczy: w danej chwili trwa najwyżej jedna
transkrypcja. Godzinny odcinek to około 21 minut. Zadania dodaje n8n przez
`POST /api/transcribe`, a nie człowiek w UI. Kontener ma ustawione `TZ=Europe/Warsaw`.

## Capabilities and Constraints

- Statusy: `queued`, `running`, `done`, `error`. Webhook ma własny stan
  (`webhook_ok`, liczba prób, błąd w polu `error`).
- Całe UI to HTML renderowany na serwerze w `app.py`. Bez builda i bez zależności frontendowych.
- Interfejs jest po polsku.
- Kształtu payloadu webhooka nie wolno ruszać, bo to kontrakt z n8n.

## Brand Commitments

Właściciel chce prosto i minimalistycznie: mało kolorów, bez animacji.

## Product Principles

- Stan widać od razu: co trwa, ile zostało, co nie wyszło.
- Błąd mówi, co się stało i jak wrócić do pracy.
- Zero konfiguracji i zero kroków budowania, tak jak w reszcie aplikacji.
