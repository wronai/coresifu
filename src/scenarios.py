"""Test scenarios for coreskill.

Each scenario simulates what a human user would do:
send commands, check responses, verify behavior.
"""
import time
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()


@dataclass
class StepResult:
    """Result of one scenario step."""
    command: str
    output: str
    duration_s: float
    passed: bool
    has_error: bool = False
    has_warning: bool = False
    check_detail: str = ""


@dataclass
class ScenarioResult:
    """Result of running a full scenario."""
    name: str
    steps: list = field(default_factory=list)
    passed: bool = True
    boot_output: str = ""
    duration_s: float = 0.0

    @property
    def failed_steps(self) -> list:
        return [s for s in self.steps if not s.passed]

    @property
    def error_steps(self) -> list:
        return [s for s in self.steps if s.has_error]


# ─── Built-in scenarios ─────────────────────────────────────

SCENARIOS = {

    "boot_health": {
        "name": "Boot & Health Check",
        "description": "Verify coreskill boots cleanly and all skills healthy",
        "critical": True,
        "steps": [
            {
                "command": "/health",
                "timeout": 15,
                "expect_any": ["OK", "HEALTHY", "✓", "zdrowy"],
                "fail_on": ["Traceback", "CRASH", "FAIL"],
            },
            {
                "command": "/skills",
                "timeout": 10,
                "expect_any": ["echo", "shell"],
                "fail_on": ["Traceback", "Error"],
            },
        ],
    },

    "echo_basic": {
        "name": "Echo Skill",
        "description": "Test basic echo skill works",
        "steps": [
            {
                "command": "/run echo test123",
                "timeout": 10,
                "expect_any": ["test123"],
                "fail_on": ["Traceback", "Error", "not found"],
            },
        ],
    },

    "chat_basic": {
        "name": "Basic Chat (LLM)",
        "description": "Test that chat with LLM works",
        "steps": [
            {
                "command": "Cześć, powiedz mi krótko co potrafisz",
                "timeout": 30,
                "expect_any": ["skill", "umiem", "potrafię", "mogę"],
                "fail_on": ["Traceback", "CRASH"],
            },
        ],
    },

    "shell_skill": {
        "name": "Shell Commands",
        "description": "Test shell skill safety and functionality",
        "steps": [
            {
                "command": "/run shell echo hello",
                "timeout": 10,
                "expect_any": ["hello"],
                "fail_on": ["Traceback", "blocked"],
            },
            {
                "command": "/run shell ls -la",
                "timeout": 10,
                "expect_any": ["total", "drwx", "drwxr"],
                "fail_on": ["Traceback"],
            },
        ],
    },

    "model_info": {
        "name": "Model & Provider Info",
        "description": "Test model listing and provider status",
        "steps": [
            {
                "command": "/models",
                "timeout": 15,
                "expect_any": ["model", "ollama", "openrouter", "free", "local"],
                "fail_on": ["Traceback"],
            },
            {
                "command": "/providers",
                "timeout": 10,
                "expect_any": ["provider", "tts", "stt", "echo"],
                "fail_on": ["Traceback"],
            },
        ],
    },

    "skill_lifecycle": {
        "name": "Skill Create & Test",
        "description": "Create, test, evolve a simple skill",
        "steps": [
            {
                "command": "/create test_supervisor_probe A simple skill that echoes its input text",
                "timeout": 60,
                "expect_any": ["created", "utworz", "skill", "evolved", "ok"],
                "fail_on": ["Traceback", "CRASH"],
            },
            {
                "command": "/test test_supervisor_probe",
                "timeout": 20,
                "expect_any": ["pass", "ok", "✓", "success", "test"],
                "fail_on": ["Traceback"],
            },
        ],
    },

    "intent_detection": {
        "name": "Intent Detection",
        "description": "Test that intents are classified correctly",
        "steps": [
            {
                "command": "wyszukaj w internecie pogodę w Gdańsku",
                "timeout": 40,
                "expect_any": ["search", "web", "szuk", "internet", "pogod", "pipe", "skill", "Naprawiam", "weather"],
                "fail_on": ["Traceback", "CRASH"],
            },
            {
                "command": "przetłumacz na angielski: cześć",
                "timeout": 40,
                "expect_any": ["hello", "hi", "tłumacz", "translat", "english", "pipe", "skill", "Naprawiam"],
                "fail_on": ["Traceback", "CRASH"],
            },
        ],
    },

    "evolve_test": {
        "name": "Evolution Engine",
        "description": "Test skill evolution works",
        "steps": [
            {
                "command": "/evolve echo Add support for reversing text when input starts with 'rev:'",
                "timeout": 90,
                "expect_any": ["evolv", "ewol", "version", "wersj", "ok", "test", "v", "fail", "skill"],
                "fail_on": ["CRASH"],
            },
        ],
    },

    "error_handling": {
        "name": "Error Handling",
        "description": "Test that errors don't crash the system",
        "steps": [
            {
                "command": "/run nonexistent_skill_12345",
                "timeout": 10,
                "expect_any": ["not found", "nie znaleziono", "brak", "unknown", "error", "Error", "FAIL", "success", "False"],
                "fail_on": ["Traceback", "CRASH"],
            },
            {
                "command": "/test nonexistent_skill_12345",
                "timeout": 15,
                "expect_any": ["not found", "nie znaleziono", "brak", "error", "fail", "FAIL", "success"],
                "fail_on": ["Traceback", "CRASH"],
            },
            {
                # Empty command should not crash
                "command": "",
                "timeout": 5,
                "expect_any": [],
                "fail_on": ["Traceback", "CRASH"],
            },
        ],
    },

    "state_persistence": {
        "name": "State & Config",
        "description": "Test state and config management",
        "steps": [
            {
                "command": "/state",
                "timeout": 15,
                "expect_any": ["state", "config", "model", "core", "{", "\""],
                "fail_on": ["Traceback"],
            },
        ],
    },

    "stress_rapid_commands": {
        "name": "Rapid Commands (stress)",
        "description": "Send multiple commands quickly to test stability",
        "steps": [
            {"command": "/health", "timeout": 10, "expect_any": [],
             "fail_on": ["Traceback", "CRASH"]},
            {"command": "/skills", "timeout": 10, "expect_any": [],
             "fail_on": ["Traceback", "CRASH"]},
            {"command": "/state", "timeout": 10, "expect_any": [],
             "fail_on": ["Traceback", "CRASH"]},
            {"command": "/health", "timeout": 10, "expect_any": [],
             "fail_on": ["Traceback", "CRASH"]},
        ],
    },
}


class ScenarioRunner:
    """Runs test scenarios against a live coreskill communicator."""

    def __init__(self, communicator, config):
        self.comm = communicator
        self.cfg = config

    def run_all(self, scenario_names: Optional[list[str]] = None
                ) -> list[ScenarioResult]:
        """Run all or selected scenarios. Returns list of results."""
        names = scenario_names or list(SCENARIOS.keys())
        results = []

        for name in names:
            if name not in SCENARIOS:
                log.warning("scenario_not_found", name=name)
                continue

            scenario = SCENARIOS[name]
            result = self.run_one(name, scenario)
            results.append(result)

            # If critical scenario fails, stop
            if scenario.get("critical") and not result.passed:
                log.error("critical_scenario_failed", name=name)
                break

        return results

    def run_one(self, name: str, scenario: dict) -> ScenarioResult:
        """Run a single scenario."""
        log.info("scenario_start", name=name,
                 steps=len(scenario.get("steps", [])))

        result = ScenarioResult(name=name)
        start = time.time()

        for step_def in scenario.get("steps", []):
            step_result = self._run_step(step_def)
            result.steps.append(step_result)

            if not step_result.passed:
                result.passed = False

        result.duration_s = round(time.time() - start, 2)

        log.info("scenario_done", name=name, passed=result.passed,
                 steps=len(result.steps),
                 failed=len(result.failed_steps),
                 duration=result.duration_s)

        return result

    def _run_step(self, step: dict) -> StepResult:
        """Execute one test step."""
        command = step["command"]
        timeout = step.get("timeout", self.cfg.command_timeout)
        expect_any = step.get("expect_any", [])
        fail_on = step.get("fail_on", [])

        resp = self.comm.send(command, timeout=timeout)

        output_lower = resp.raw.lower()
        passed = True
        detail = ""

        # Check fail conditions (fatal patterns)
        for pattern in fail_on:
            if pattern.lower() in output_lower:
                passed = False
                detail = f"Found fail pattern: '{pattern}'"
                break

        # Check expect conditions (at least one must match)
        if passed and expect_any:
            found = any(exp.lower() in output_lower for exp in expect_any)
            if not found:
                passed = False
                detail = (
                    f"Expected one of {expect_any}, "
                    f"got: {resp.raw[:200]}"
                )

        # Timeout check
        if not resp.prompt_seen:
            passed = False
            detail = f"No prompt after {timeout}s (command hung?)"

        return StepResult(
            command=command,
            output=resp.raw,
            duration_s=resp.duration_s,
            passed=passed,
            has_error=resp.has_error,
            has_warning=resp.has_warning,
            check_detail=detail,
        )

    def load_custom_scenarios(self, path: Path) -> dict:
        """Load additional scenarios from YAML file."""
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                custom = yaml.safe_load(f) or {}
            SCENARIOS.update(custom)
            log.info("custom_scenarios_loaded", count=len(custom), path=str(path))
            return custom
        except Exception as e:
            log.warning("custom_scenarios_error", error=str(e))
            return {}


def format_report(results: list[ScenarioResult]) -> str:
    """Format scenario results as human-readable report."""
    lines = ["=" * 60, "EVO-SUPERVISOR TEST REPORT", "=" * 60, ""]

    total_steps = sum(len(r.steps) for r in results)
    failed_steps = sum(len(r.failed_steps) for r in results)
    passed_scenarios = sum(1 for r in results if r.passed)

    lines.append(
        f"Scenarios: {passed_scenarios}/{len(results)} passed | "
        f"Steps: {total_steps - failed_steps}/{total_steps} passed"
    )
    lines.append("")

    for result in results:
        icon = "✓" if result.passed else "✗"
        lines.append(f"{icon} {result.name} ({result.duration_s}s)")

        for step in result.steps:
            step_icon = "  ✓" if step.passed else "  ✗"
            cmd_display = step.command[:50] if step.command else "(empty)"
            lines.append(f"  {step_icon} {cmd_display} ({step.duration_s}s)")
            if not step.passed:
                lines.append(f"      → {step.check_detail}")
            if step.has_error:
                lines.append(f"      ⚠ Errors in output")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)
