# Evo-Supervisor: Zewnętrzny Core Auto-Doskonalenia

**Projekt:** evo-supervisor | **Data:** 2026-03-04  
**Status:** Nowy projekt — outer core dla coreskill  
**LOC:** 2 272 (Python) + 410 (config/Docker) = 2 682 total  
**Pliki:** 10 Python + 7 config

---

## Problem

Coreskill ma wbudowane mechanizmy samonaprawy (SelfReflection, AutoRepair, EvolutionGuard), ale mają fundamentalne ograniczenie: **nie mogą naprawić samych siebie.** Jeśli bug jest w SelfReflection.py, to SelfReflection nie wykryje buga w SelfReflection. Jak chirurg nie może operować samego siebie.

Aktualnie to człowiek (developer) pełni rolę zewnętrznego obserwatora: uruchamia system, testuje, czyta logi, edytuje kod, weryfikuje. Ten projekt automatyzuje dokładnie tę rolę.

## Rozwiązanie — Architektura

Evo-supervisor to **oddzielny projekt**, który traktuje coreskill jak black box. Komunikuje się z nim wyłącznie przez stdin/stdout (tekst), jak człowiek w terminalu.

```
evo-supervisor (nowy projekt)
  │
  │  stdin: "/health\n"
  │  stdin: "cześć\n"
  │  stdin: "/run echo test\n"
  ▼
┌──────────────────────┐
│  Docker container     │
│  coreskill            │
│  python3 main.py      │
│  stdin ←→ stdout      │
└──────────────────────┘
  │
  │  stdout: "[HEALTH] ⚠ web_search..."
  │  stdout: "Traceback..."
  ▼
evo-supervisor czyta output
  → Analyzer (LLM) diagnozuje
  → Surgeon edytuje pliki .py
  → rebuild + re-test
  → git commit jeśli OK
```

## Cykl Pracy (identyczny z ludzkim developerem)

**BUILD** → Docker image z aktualnym kodem coreskill. Albo subprocess bez Dockera (fallback).

**BOOT** → Uruchom, czekaj na prompt `you>`. Timeout 60s. Jeśli Traceback przy starcie — issue severity=critical.

**TEST** → 10 wbudowanych scenariuszy symulujących człowieka: komendy (/health, /skills, /run echo), chat po polsku, edge cases (emoji, puste komendy, nieistniejące skille), stress test (4 komendy szybko).

**DIAGNOSE** → Dwie warstwy. Rule-based: regex na Traceback, `[HEALTH] ⚠`, timeout >25s. LLM-based: wysyła cały output do Claude/Llama z promptem "znajdź bugi w tym output evo-engine". LLM zwraca JSON z listą issues, severity, affected_files, suggested_fix.

**PLAN** → Dla każdego issue severity=critical/error: czytaj affected files, wyślij do LLM z promptem "zaplanuj patch". LLM zwraca JSON z dokładnymi search/replace operacjami na kodzie. Confidence score — skip jeśli <0.4.

**PATCH** → Surgeon aplikuje zmiany z safety: backup → apply → syntax check (compile()) → rollback jeśli SyntaxError. Fuzzy matching jeśli LLM pomyli whitespace (SequenceMatcher ≥70%).

**VERIFY** → Rebuild + boot + re-run krytyczne scenariusze. Pass → git merge. Fail → git reset + rollback wszystkich zmian.

**COMMIT** → Każdy fix na osobnym git branchu (supervisor/fix-{issue}-{timestamp}). Merge do main tylko po weryfikacji. Journal zapisuje co próbowaliśmy i czy zadziałało.

## Komponenty

| Plik | LOC | Rola |
|------|-----|------|
| `main.py` | 465 | Orkiestrator — główna pętla cykli |
| `scenarios.py` | 384 | 10 scenariuszy testowych + runner |
| `analyzer.py` | 344 | LLM diagnoza + planowanie fixów |
| `surgeon.py` | 310 | Edycja kodu z backup/rollback/syntax |
| `docker_runner.py` | 191 | Zarządzanie kontenerem Docker |
| `communicator.py` | 173 | stdin/stdout z procesem coreskill |
| `git_manager.py` | 169 | Branch/commit/merge |
| `journal.py` | 169 | Pamięć — nie powtarzaj failed fixów |
| `config.py` | 66 | Konfiguracja |

## Safety

System jest konserwatywny — lepiej nie naprawić niż zepsuć:

**Backup zawsze** — każdy plik backupowany przed edycją, z timestampem. Restore w <1s.

**Syntax gate** — po każdym patchu `compile(code, "<patch>", "exec")`. Jeśli SyntaxError → natychmiastowy rollback, patch odrzucony.

**Confidence threshold** — LLM musi dać ≥0.4 confidence. Poniżej = skip. Nie próbujemy napraw "na ślepo".

**Max 3 patche per cykl** — nie zmieniaj za dużo naraz. Łatwiej debugować.

**Forbidden paths** — nie rusza main.py (entry point), .git/, __pycache__/, state files.

**Verify before merge** — po patchu pełny rebuild + boot + testy. Merge do main TYLKO jeśli testy przejdą.

**Journal memory** — jeśli fix był już próbowany i failed, nie próbuj ponownie. Zapobiega nieskończonym pętlom (ten sam problem co STT circuit breaker w coreskill).

## Scenariusze Testowe

10 wbudowanych scenariuszy pokrywa kluczowe ścieżki:

**boot_health** (CRITICAL) — boot musi się udać, /health musi odpowiedzieć. Jeśli fail → stop testów.

**echo_basic** — najprostszy skill. Jeśli echo nie działa, nic nie działa.

**chat_basic** — rozmowa z LLM po polsku. Weryfikuje intent detection + LLM routing.

**shell_skill** — echo hello + ls. Weryfikuje subprocess handling.

**error_handling** — nieistniejące skille i komendy. System NIE MOŻE crashować na złym input.

**intent_detection** — "wyszukaj pogodę" → web_search, "powiedz na głos" → TTS.

**stress_rapid_commands** — 4 komendy szybko po sobie. Race conditions, buffer overflows.

Dodatkowe scenariusze w `scenarios/custom.yaml`: polish commands, benchmark, web search, edge cases (emoji, single char).

## Różnica vs Wbudowane SelfReflection

| Cecha | SelfReflection (wewnętrzne) | evo-supervisor (zewnętrzne) |
|-------|---------------------------|----------------------------|
| Widzi własne bugi | Nie | Tak — czyta kod z zewnątrz |
| Edytuje własny kod | Nie | Tak — Surgeon pisze do plików |
| Persystencja | W procesie | Git + journal (przeżywa restart) |
| Weryfikacja | Brak post-repair check | Rebuild + re-test |
| Timeout protection | Brak (nieskończone pętle) | Command timeout + boot timeout |
| Duplikacja napraw | web_search naprawiany 2× | Journal dedup |
| Eskalacja | Brak | Skip po failed attempts |

## Następne Kroki

1. **Integracja z code2llm** — przed planowaniem fixów, uruchom analizę statyczną (CC, duplikaty) żeby LLM miał pełny obraz
2. **Regression tracking** — porównuj wyniki testów między cyklami. Alert jeśli scenario przeszedł → przestał przechodzić
3. **Multi-model judge** — diagnostyka dwoma modelami niezależnie, consensus wymagany dla critical patches
4. **Scheduled cron** — uruchamiaj co noc, rano masz commity z opisami co naprawiono

---

*Projekt: 10 plików Python (2 272 LOC), 2 Dockerfile, docker-compose, 10 scenariuszy testowych. Zero zewnętrznych zależności poza litellm, structlog, pyyaml, GitPython. Data: 2026-03-04.*
