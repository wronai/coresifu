## [1.0.2] - 2026-03-04

### Summary

fix(core): stabilize supervisor cycle after command timeouts and improve patch safety

### Changes

- communicator: resync to next prompt after command timeout (prevents stale output contaminating next step)
- surgeon: rollback scope reset per apply_plan() (prevents unrelated patches being undone)
- journal: reduce false positives in was_tried() matching
- analyzer: better source truncation by centering around evidence
- docker_runner: random container name per run (avoids parallel conflicts)

## [1.0.1] - 2026-03-04

### Summary

feat(goal): deep code analysis engine with 7 supporting modules

### Test

- update tests/test_coresifu.py

### Config

- config: update goal.yaml


