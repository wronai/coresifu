# evo-supervisor

**Zewnętrzny core auto-doskonalenia** — zastępuje Ciebie jako developera. Uruchamia coreskill w Docker, komunikuje się z nim jak człowiek (tekst stdin/stdout), diagnozuje błędy LLM-em, edytuje kod źródłowy, weryfikuje poprawki i commituje.

## Idea

Ty robisz dziś:
```
1. Uruchamiasz coreskill
2. Wpisujesz komendy, testujesz
3. Widzisz błąd
4. Otwierasz plik, czytasz kod
5. Naprawiasz
6. Testujesz znowu
7. Commit
```

evo-supervisor robi dokładnie to samo, automatycznie, w pętli:

```
┌───────────────────────────────────────────────────────┐
│  evo-supervisor (outer core, "lustrzany JA")          │
│                                                       │
│  ┌───────────┐  stdin   ┌──────────────────────────┐ │
│  │ Scenario  │ ──────→  │  Docker / subprocess     │ │
│  │ Runner    │ ←──────  │  coreskill               │ │
│  │ (tester)  │  stdout  │  python3 main.py         │ │
│  └─────┬─────┘          └──────────────────────────┘ │
│        │ output                                       │
│  ┌─────▼─────┐   issues   ┌────────────┐            │
│  │ Analyzer  │ ─────────→ │  Surgeon   │            │
│  │ (LLM)     │            │ (patcher)  │            │
│  └───────────┘            └─────┬──────┘            │
│                                 │ edits files        │
│  ┌───────────┐            ┌─────▼──────┐            │
│  │ Journal   │ ←───────── │ Git Mgr    │            │
│  │ (memory)  │            │ (commits)  │            │
│  └───────────┘            └────────────┘            │
└───────────────────────────────────────────────────────┘
```

## Cykl działania

```
  ┌─ BUILD ── Docker image z kodem coreskill
  │
  ├─ BOOT ─── Uruchom kontener, czekaj na "you> " prompt
  │
  ├─ TEST ─── Wyślij scenariusze: /health, /skills, chat, edge cases
  │            Jak człowiek — wpisuje komendy, czyta odpowiedzi
  │
  ├─ DIAGNOSE ─ Rule-based: szukaj Traceback, ⚠, timeout
  │              LLM-based: "co jest nie tak w tym output?"
  │
  ├─ PLAN ──── LLM czyta kod źródłowy + issue → planuje patch
  │             Confidence score — skip jeśli < 0.4
  │
  ├─ PATCH ─── Surgeon edytuje pliki .py w repo coreskill
  │             Backup → apply → syntax check → rollback on error
  │
  ├─ VERIFY ── Rebuild → boot → re-run testy → pass?
  │             ✓ → commit + merge
  │             ✗ → rollback + journal "failed fix"
  │
  └─ REPEAT ── Następny cykl z nowym stanem
```

## Szybki start

```bash
# 1. Klonuj obok coreskill
git clone <this-repo> evo-supervisor
cd evo-supervisor

# 2. Ustaw klucz API
export OPENROUTER_API_KEY=sk-or-...

# 3. Uruchom
chmod +x run.sh
./run.sh --path ../coreskill --cycles 5

# Lub tylko testy (bez naprawiania):
./run.sh --path ../coreskill --test-only
```

### Tryby uruchomienia

```bash
# Pełna pętla: test → diagnose → fix → verify → commit
python -m src.main -p ../coreskill -n 10

# Tylko testy — raport bez zmian w kodzie
python -m src.main -p ../coreskill --test-only

# Konkretne scenariusze
python -m src.main -p ../coreskill --test-only -s boot_health echo_basic

# Lista scenariuszy
python -m src.main --list-scenarios

# Docker compose (oba w kontenerach)
CORESKILL_PATH=../coreskill docker compose up --build
```

## Architektura plików

```
evo-supervisor/
├── run.sh                    # launcher
├── Dockerfile.coreskill      # image dla coreskill
├── Dockerfile.supervisor     # image dla supervisora
├── docker-compose.yml        # oba razem
├── requirements.txt
├── scenarios/
│   └── custom.yaml           # dodatkowe scenariusze
├── src/
│   ├── main.py               # ORKIESTRATOR — główna pętla
│   ├── config.py             # ustawienia
│   ├── docker_runner.py      # zarządza kontenerem Docker
│   ├── communicator.py       # stdin/stdout z coreskill
│   ├── scenarios.py          # scenariusze testowe (10 built-in)
│   ├── analyzer.py           # LLM diagnoza + planowanie fixów
│   ├── surgeon.py            # edycja kodu z backup/rollback
│   ├── git_manager.py        # branch/commit/merge
│   └── journal.py            # pamięć — co próbowaliśmy, co działało
├── workspace/                # backupy plików
├── logs/                     # raporty z cykli
└── patches/                  # wygenerowane diffy
```

## Scenariusze testowe (built-in)

| Nazwa | Co testuje | Krytyczny? |
|-------|------------|:----------:|
| `boot_health` | Boot + /health + /skills | ✓ |
| `echo_basic` | /run echo test | |
| `chat_basic` | Rozmowa po polsku z LLM | |
| `shell_skill` | Shell echo + ls | |
| `model_info` | /models + /providers | |
| `skill_lifecycle` | /create + /test | |
| `intent_detection` | Klasyfikacja intencji PL | |
| `evolve_test` | /evolve echo | |
| `error_handling` | Nieistniejące komendy/skille | |
| `stress_rapid_commands` | 4 komendy szybko | |

## Safety

Surgeon (edytor kodu) ma zabezpieczenia:

- **Backup** przed każdą zmianą (timestamped)
- **Syntax check** po patchu — rollback jeśli zepsuty Python
- **Forbidden paths** — nie rusza main.py, .git/, state
- **Max patches per cycle** — domyślnie 3
- **Confidence threshold** — LLM musi mieć ≥0.4 pewności
- **Fuzzy match** — jeśli LLM pomyli whitespace, znajdzie ~70% match
- **Git branch per fix** — każdy fix na osobnym branchu
- **Verify before merge** — re-test po patchu, merge tylko jeśli pass
- **Journal** — pamięta co próbowaliśmy, nie powtarza failed fixów

## Konfiguracja

```bash
# Wymagane
OPENROUTER_API_KEY=sk-or-...

# Opcjonalne
CORESKILL_PATH=../coreskill              # ścieżka do repo
SUPERVISOR_MODEL=openrouter/anthropic/claude-sonnet-4-20250514  # model do analizy
```

## Dodawanie własnych scenariuszy

Utwórz plik YAML w `scenarios/`:

```yaml
moj_test:
  name: "Mój test"
  description: "Testuje specyficzną funkcjonalność"
  critical: false
  steps:
    - command: "/health"
      timeout: 15
      expect_any: ["OK", "healthy"]
      fail_on: ["Traceback", "CRASH"]
```
