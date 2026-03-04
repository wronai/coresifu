"""LLM-powered analysis of coreskill output."""
import json
import re
from dataclasses import dataclass, field
from typing import Optional

import litellm
import structlog

log = structlog.get_logger()


@dataclass
class Issue:
    """A single detected issue."""
    severity: str  # critical, error, warning, info
    category: str  # crash, health, performance, logic, regression
    description: str
    evidence: str  # the actual output that shows the issue
    affected_files: list = field(default_factory=list)
    suggested_fix: str = ""


@dataclass
class Diagnosis:
    """Complete diagnosis from analyzing a session."""
    issues: list = field(default_factory=list)
    boot_ok: bool = True
    health_ok: bool = True
    commands_tested: int = 0
    commands_failed: int = 0
    overall_health: str = "unknown"  # healthy, degraded, broken
    raw_analysis: str = ""


ANALYZE_SYSTEM = """You are an expert Python developer analyzing the runtime output of "evo-engine" (coreskill) — a self-evolving AI system.

Your job: find bugs, errors, regressions, and improvement opportunities.

evo-engine structure:
- main.py → bootstrap → cores/v1/core.py (main loop)
- cores/v1/ → config, evo_engine, intent_engine, llm_client, skill_manager, etc.
- skills/ → echo, shell, stt, tts, benchmark, web_search, etc.
- Each skill has: skill.py, get_info(), health_check(), execute()

Respond ONLY with valid JSON matching this schema:
{
  "boot_ok": true/false,
  "health_ok": true/false,
  "overall": "healthy" | "degraded" | "broken",
  "issues": [
    {
      "severity": "critical" | "error" | "warning",
      "category": "crash" | "health" | "performance" | "logic" | "regression",
      "description": "what is wrong",
      "evidence": "exact line(s) from output showing the problem",
      "affected_files": ["cores/v1/file.py", ...],
      "suggested_fix": "brief description of how to fix"
    }
  ]
}"""

PLAN_SYSTEM = """You are an expert Python developer. You will receive:
1. A diagnosed issue in evo-engine (coreskill)
2. The source code of affected file(s)

Your job: produce a PRECISE code patch to fix the issue.

Rules:
- Fix the ROOT CAUSE, not symptoms
- Make minimal changes — touch only what's needed
- Preserve all existing functionality
- Add comments explaining the fix
- If the fix requires adding imports, include them

Respond ONLY with valid JSON:
{
  "confidence": 0.0-1.0,
  "explanation": "why this fix works",
  "patches": [
    {
      "file": "relative/path/to/file.py",
      "action": "replace" | "insert_after" | "insert_before" | "delete",
      "search": "exact string to find (for replace/insert/delete)",
      "replace": "replacement string (for replace action)",
      "content": "content to insert (for insert actions)"
    }
  ],
  "test_command": "command to verify the fix works (e.g., '/health')",
  "expected_output": "what success looks like"
}"""


class Analyzer:
    """Analyzes coreskill output using LLM."""

    def __init__(self, config):
        self.cfg = config
        self.model = config.supervisor_model
        self.fallback = config.fallback_model

    def _llm_call(self, system: str, user: str,
                   max_tokens: int = 2000) -> Optional[str]:
        """Make LLM call with fallback."""
        for model in [self.model, self.fallback]:
            try:
                resp = litellm.completion(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.1,
                    max_tokens=max_tokens,
                    api_key=self.cfg.api_key,
                    timeout=60,
                )
                text = resp.choices[0].message.content or ""
                log.info("llm_call_ok", model=model.split("/")[-1],
                         tokens=len(text))
                return text
            except Exception as e:
                log.warning("llm_call_failed", model=model, error=str(e)[:80])
                continue
        return None

    def _parse_json(self, text: str) -> Optional[dict]:
        """Extract JSON from LLM response (handles markdown fences)."""
        if not text:
            return None

        # Strip markdown fences
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON object in text
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
        return None

    def diagnose(self, boot_output: str,
                  command_results: list[dict]) -> Diagnosis:
        """Analyze full session output and return diagnosis."""

        # Build context for LLM
        session_log = f"=== BOOT OUTPUT ===\n{boot_output[-3000:]}\n\n"
        for cr in command_results[-20:]:  # last 20 commands
            session_log += (
                f"=== COMMAND: {cr['command']} ===\n"
                f"{cr['output'][-1000:]}\n"
                f"Duration: {cr['duration_s']}s | "
                f"Error: {cr['has_error']} | "
                f"Warning: {cr['has_warning']}\n\n"
            )

        # Rule-based pre-analysis (fast, no LLM)
        diagnosis = self._rule_based_analysis(boot_output, command_results)

        # LLM deep analysis
        llm_text = self._llm_call(
            ANALYZE_SYSTEM,
            f"Analyze this evo-engine session:\n\n{session_log}",
            max_tokens=3000,
        )

        if llm_text:
            diagnosis.raw_analysis = llm_text
            parsed = self._parse_json(llm_text)
            if parsed:
                diagnosis.boot_ok = parsed.get("boot_ok", diagnosis.boot_ok)
                diagnosis.health_ok = parsed.get("health_ok", diagnosis.health_ok)
                diagnosis.overall_health = parsed.get("overall", "unknown")

                for issue_data in parsed.get("issues", []):
                    issue = Issue(
                        severity=issue_data.get("severity", "warning"),
                        category=issue_data.get("category", "logic"),
                        description=issue_data.get("description", ""),
                        evidence=issue_data.get("evidence", ""),
                        affected_files=issue_data.get("affected_files", []),
                        suggested_fix=issue_data.get("suggested_fix", ""),
                    )
                    # Deduplicate with rule-based
                    if not any(i.description == issue.description
                               for i in diagnosis.issues):
                        diagnosis.issues.append(issue)

        # Sort by severity
        severity_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
        diagnosis.issues.sort(
            key=lambda i: severity_order.get(i.severity, 9)
        )

        log.info("diagnosis_complete",
                 issues=len(diagnosis.issues),
                 health=diagnosis.overall_health)

        return diagnosis

    def _rule_based_analysis(self, boot_output: str,
                              command_results: list[dict]) -> Diagnosis:
        """Fast rule-based analysis without LLM."""
        diag = Diagnosis()

        # Check boot
        if "Traceback" in boot_output:
            diag.boot_ok = False
            diag.issues.append(Issue(
                severity="critical",
                category="crash",
                description="Crash during boot",
                evidence=self._extract_traceback(boot_output),
            ))

        if "you>" not in boot_output and "evo>" not in boot_output:
            diag.boot_ok = False
            diag.issues.append(Issue(
                severity="critical",
                category="crash",
                description="Boot did not complete — no prompt appeared",
                evidence=boot_output[-500:],
            ))

        # Check health
        health_issues = re.findall(
            r"\[HEALTH\]\s*⚠\s*(.+)", boot_output
        )
        for hi in health_issues:
            diag.health_ok = False
            skill_name = hi.split(":")[0].strip()
            diag.issues.append(Issue(
                severity="warning",
                category="health",
                description=f"Skill unhealthy: {hi}",
                evidence=f"[HEALTH] ⚠ {hi}",
                affected_files=[f"skills/{skill_name}/"],
            ))

        # Check repair failures
        repair_fails = re.findall(
            r"\[REPAIR\]\s*✗\s*(.+)", boot_output
        )
        for rf in repair_fails:
            diag.issues.append(Issue(
                severity="error",
                category="health",
                description=f"Auto-repair failed: {rf}",
                evidence=f"[REPAIR] ✗ {rf}",
            ))

        # Check command results
        for cr in command_results:
            diag.commands_tested += 1
            if cr.get("has_error"):
                diag.commands_failed += 1

            # Detect infinite loops
            if cr.get("duration_s", 0) > 25:
                diag.issues.append(Issue(
                    severity="warning",
                    category="performance",
                    description=(
                        f"Command took {cr['duration_s']}s: {cr['command']}"
                    ),
                    evidence=cr.get("output", "")[-200:],
                ))

            # Detect tracebacks in command output
            output = cr.get("output", "")
            if "Traceback" in output:
                diag.issues.append(Issue(
                    severity="error",
                    category="crash",
                    description=(
                        f"Traceback in response to: {cr['command']}"
                    ),
                    evidence=self._extract_traceback(output),
                ))

        diag.overall_health = (
            "broken" if not diag.boot_ok
            else "degraded" if diag.issues
            else "healthy"
        )

        return diag

    def plan_fix(self, issue: Issue, file_contents: dict[str, str]) -> Optional[dict]:
        """Plan a code fix for a specific issue.

        Args:
            issue: The issue to fix
            file_contents: {filename: content} of affected files
        """
        context = f"""ISSUE:
  Severity: {issue.severity}
  Category: {issue.category}
  Description: {issue.description}
  Evidence: {issue.evidence}
  Suggested fix direction: {issue.suggested_fix}

AFFECTED FILES:
"""
        for fname, content in file_contents.items():
            # Limit file size
            if len(content) > 8000:
                content = content[:4000] + "\n... (truncated) ...\n" + content[-4000:]
            context += f"\n--- {fname} ---\n{content}\n"

        llm_text = self._llm_call(PLAN_SYSTEM, context, max_tokens=4000)

        if not llm_text:
            return None

        parsed = self._parse_json(llm_text)
        if not parsed:
            log.warning("plan_fix_unparseable", issue=issue.description)
            return None

        log.info("fix_planned",
                 issue=issue.description[:60],
                 confidence=parsed.get("confidence", 0),
                 patches=len(parsed.get("patches", [])))

        return parsed

    def _extract_traceback(self, text: str) -> str:
        """Extract the last traceback from output."""
        lines = text.splitlines()
        tb_start = None
        for i, line in enumerate(lines):
            if "Traceback" in line:
                tb_start = i
        if tb_start is not None:
            return "\n".join(lines[tb_start:tb_start + 20])
        return ""
