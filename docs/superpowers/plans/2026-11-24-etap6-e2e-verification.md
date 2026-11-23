# ETAP 6 — Weryfikacja end-to-end i bramki bezpieczeństwa (2026-11-24)

GO: implementacja OD-E1..OD-E4 + pełna macierz e2e. Baseline: 39e2ac1, 300 passed.
Final: 423 passed (x2 stabilnie), ruff clean na wszystkich plikach zmiany, DB produkcyjna nietknięta
(md5 a830293f03f13b2e28396b5360ebc0f5, mtime 2026-11-05 12:22:45), zero commitów agenta.

## Delegacje (route developer = openai/gpt-6-luna, postcheck=0; reviewer = qwen/qwen3.8-flash)
- A: registered→seal (OD-E2), D-3, D-4 — weryfikacja rodzica OK.
- A2: grant-v1 hard gate (OD-E1) — A zwęził zakres, domknięte dedykowanym dzieckiem.
- C: limity SOFT 256MiB/2GiB/200x, streaming, wspólny _limited_gunzip (OD-E3).
- B: tests/e2e_harness.py + 45 testów e2e (6 wyników + crash boundaries + deferred/75 + seal immutability).
- Review runda 1 (niezależny): FAIL — P1-1 brak re-checku artefaktów w save-characteristics (udowodnione
  race-probe'em rodzica), P1-2 tools ModuleNotFoundError na plain path + rozjazd SKILL.md vs CLI (rodzic
  potwierdził: --help real vs flags w skillu), P1-3 sfabrykowany stub refuse w harness + realny łańcuch
  refuse→seal nie-do-sealenia (rodzic: write_text w harness:285-289; probe refuse→seal = E_HANDOFF_MISSING).
  Plus P2-a..P2-f.
- D (fix round 1): zamknął wszystkie 10 pozycji + tabelę komend spiętą kontraktem --help.
- Review runda 2 (FINAL, limit): PASS, 0 P0/P1, 7 P2 (1 regresja fix-ronda: validate/seal crash na
  non-dict qualification — naprawiona rodzicem inline wraz z JSON-kontraktem seal; reszta = backlog).
  Niezależne probe'y recenzenta: race 3/3 komend, socket-guard bez maskowania, bootstrap bez shadowingu,
  łańcuch refuse odtworzony od zera bez harnessa, run-id binding, envelope-relative report.
- Po PASS rodzic: _sec isinstance + cli_seal try/except EnvelopeError→JSON exit 1 (adaptacja 1 testu D
  do nowego kontraktu — nie osłabienie), usunięcie martwej stałej MAX_COMPRESSED_BYTES, korekta linii
  recover w SKILL.md; 1× ruff PLW1510 (check=False) domknięte.

## Odstępstwa i znane limity (backlog)
- P2-2: non-dict `handoff` nadal crashuje walidator (pre-existing od HEAD) — dokończyć wzorcem _sec.
- P2-5: podwójne E_REGISTERED_ORDER+MISMATCH na jednym błędzie (fail-closed, kosmetyka).
- P2-6: `refuse --stage` bez click.Choice — destrukcyjne ale bramy dalej fail-closed (pre-existing).
- P2-7: samo-anulujący się warunek walidacji qualification.errors (pre-existing, exit 0 na
  errors:["boom"]).
- Step-10 registered = plik-trust (zgodnie ze spec §12.2: envelope NIE czyta DuckDB); wzmocnienie
  opcjonalne: carry grant_sha256 w registration-v1 i weryfikacja przy seal.
- persistence jest opt-in: run bez bloku persistence nie wymaga zdarzeń registered (zatwierdzone OD-E2).
- /tmp = 512MiB tmpfs: pełny download 256MiB `.part` może ulec ENOSPC — warunek środowiskowy, nie wada;
  stream .part ląduje w run-dir (kontrolowany) dla --run-dir, w TMPDIR dla plain API.
- Zmiana pozarepo: profil /opt/data/skills/research/geo-dataset-production-pipeline zredukowany do
  wskaźnika na repo sc-dataset-pipeline (OD-E4; niewidoczny w git diff — udokumentowane; read-back OK).

## Kryteria akceptacji §7: wszystkie 17 zweryfikowane na zielono (patrz raport końcowy w wątku).
