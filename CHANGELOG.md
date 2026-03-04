## [1.0.2] - 2026-03-04

### Summary

fix(core): stabilize supervisor cycle after command timeouts and improve patch safety

### Changes

- communicator: resync to next prompt after command timeout (prevents stale output contaminating next step)
- communicator: add retry logic with exponential backoff for broken pipe errors
- surgeon: rollback scope reset per apply_plan() (prevents unrelated patches being undone)
- journal: reduce false positives in was_tried() matching
- analyzer: better source truncation by centering around evidence
- docker_runner: random container name per run (avoids parallel conflicts)
- main: add try/finally blocks for proper resource cleanup on exit
- main: add graceful KeyboardInterrupt handling with proper shutdown

### Coreskill Improvements

- core: add SIGTERM/SIGINT signal handling for graceful shutdown
- skill_manager: add timeout support (default 30s) for skill execution using ThreadPoolExecutor
- llm_client: add specific timeout error handling with automatic retry for local models
- config: fix bare except clause to catch specific exceptions

## [1.0.1] - 2026-03-04

### Summary

feat(goal): deep code analysis engine with 7 supporting modules

### Test

- update tests/test_coresifu.py

### Config

- config: update goal.yaml


