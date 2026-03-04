"""evo-supervisor main loop.

This is the "mirror developer" — it does exactly what a human would:
1. Start coreskill
2. Test everything
3. Find problems
4. Read the code
5. Plan a fix
6. Apply the fix
7. Test again to verify
8. Commit if it works, rollback if not
9. Repeat
"""
import sys
import time
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)

log = structlog.get_logger()

from .config import Config
from .docker_runner import create_runner, SubprocessRunner
from .communicator import Communicator
from .analyzer import Analyzer, Issue
from .surgeon import Surgeon
from .git_manager import GitManager
from .scenarios import ScenarioRunner, format_report, SCENARIOS
from .journal import Journal


class Supervisor:
    """The outer core. Runs improvement cycles on coreskill.

    Each cycle:
      boot → test → diagnose → [plan → patch → verify → commit] → stop
    """

    def __init__(self, config: Config, runner_factory=None):
        self.cfg = config
        runner_factory = runner_factory or create_runner
        self.runner = runner_factory(config)
        self.analyzer = Analyzer(config)
        self.surgeon = Surgeon(config)
        self.git = GitManager(config)
        self.journal = Journal(config.logs_dir)
        self._comm = None
        self._proc = None

    def run(self, max_cycles: int = None):
        """Run the full improvement loop."""
        max_cycles = max_cycles or self.cfg.max_cycles

        log.info("supervisor_start",
                 coreskill=str(self.cfg.coreskill_path),
                 max_cycles=max_cycles,
                 model=self.cfg.supervisor_model.split("/")[-1])

        # Ensure git is initialized
        self.git.init_if_needed()

        # Build Docker image (once)
        if not self.runner.build():
            log.error("build_failed")
            return

        for cycle_idx in range(1, max_cycles + 1):
            log.info("cycle_start", cycle=cycle_idx, of=max_cycles)
            outcome = self._run_one_cycle(cycle_idx)

            if outcome == "healthy":
                log.info("system_healthy_stopping", cycle=cycle_idx)
                break
            elif outcome == "fatal":
                log.error("fatal_error_stopping", cycle=cycle_idx)
                break

            # Brief pause between cycles
            if cycle_idx < max_cycles:
                time.sleep(2)

        # Final report
        print("\n" + "=" * 60)
        print("SUPERVISOR COMPLETE")
        print("=" * 60)
        print(self.journal.summary())

    def _run_one_cycle(self, cycle_num: int) -> str:
        """Run one improvement cycle. Returns outcome string."""
        journal_id = self.journal.start_cycle()
        try:
            boot_resp = self._phase_boot(journal_id)
            if boot_resp is None:
                return "fatal" if not self._proc else "error"

            results, report = self._phase_test(journal_id)
            diagnosis = self._phase_diagnose(boot_resp, results, journal_id)

            fixable = [
                i for i in diagnosis.issues
                if i.severity in ("critical", "error") and i.affected_files
            ]
            if not fixable:
                self._shutdown()
                if diagnosis.overall_health == "healthy":
                    log.info("all_healthy_nothing_to_fix")
                    self.journal.finish_cycle(journal_id, "healthy")
                    return "healthy"
                log.info("issues_found_but_no_fix_targets",
                         issues=len(diagnosis.issues))
                self.journal.finish_cycle(journal_id, "no_fixable_issues")
                return "continue"

            return self._phase_fix_and_verify(fixable, journal_id, cycle_num)

        except KeyboardInterrupt:
            log.info("interrupted")
            self._shutdown()
            self.journal.finish_cycle(journal_id, "interrupted")
            return "fatal"
        except Exception as e:
            log.error("cycle_error", error=str(e))
            self._shutdown()
            self.journal.finish_cycle(journal_id, f"error: {e}")
            return "error"

    def _phase_boot(self, journal_id: int):
        """Boot coreskill and wait for prompt. Returns boot Response or None."""
        log.info("step_boot")
        self._proc = self.runner.start()
        if not self._proc:
            self.journal.finish_cycle(journal_id, "boot_failed")
            return None

        self._comm = Communicator(self._proc, self.cfg)
        boot_resp = self._comm.wait_for_boot(self.cfg.boot_timeout)

        if not boot_resp.prompt_seen:
            log.error("boot_no_prompt", output=boot_resp.raw[-300:])
            self._shutdown()
            self.journal.finish_cycle(journal_id, "boot_timeout")
            return None

        log.info("boot_ok", duration=boot_resp.duration_s)
        return boot_resp

    def _phase_test(self, journal_id: int):
        """Run all scenarios. Returns (results, report_text)."""
        log.info("step_test")
        scenario_runner = ScenarioRunner(self._comm, self.cfg)
        results = scenario_runner.run_all()
        report = format_report(results)
        print(report)

        report_path = self.cfg.logs_dir / f"cycle_{journal_id}_report.txt"
        report_path.write_text(report)
        return results, report

    def _phase_diagnose(self, boot_resp, results, journal_id: int):
        """Analyze results and return Diagnosis."""
        log.info("step_diagnose")
        command_results = [
            {
                "command": step.command,
                "output": step.output,
                "duration_s": step.duration_s,
                "has_error": step.has_error,
                "has_warning": step.has_warning,
            }
            for sr in results
            for step in sr.steps
        ]
        diagnosis = self.analyzer.diagnose(boot_resp.raw, command_results)
        self.journal.record_issues(journal_id, [
            {"severity": i.severity, "description": i.description,
             "category": i.category}
            for i in diagnosis.issues
        ])
        return diagnosis

    def _phase_fix_and_verify(self, fixable: list, journal_id: int,
                               cycle_num: int) -> str:
        """Apply patches for fixable issues, then verify. Returns outcome."""
        log.info("step_fix", fixable=len(fixable))
        self._shutdown()  # stop coreskill before editing files

        patches_applied = 0
        for issue in fixable[:self.cfg.max_patches_per_cycle]:
            prior = self.journal.was_tried(issue.description)
            if prior and not prior["success"]:
                log.info("skip_known_failure", issue=issue.description[:60])
                continue
            if self._fix_one_issue(issue, journal_id):
                patches_applied += 1

        if patches_applied == 0:
            self.journal.finish_cycle(journal_id, "no_patches_applied")
            return "continue"

        log.info("step_verify")
        verified = self._verify_fixes(journal_id)

        if verified:
            self.git.commit(
                f"supervisor: fix {patches_applied} issues (cycle {journal_id})"
            )
            self.journal.record_verification(journal_id, True)
            self.journal.finish_cycle(journal_id, "success")
            log.info("cycle_success", patches=patches_applied, cycle=cycle_num)
        else:
            log.warning("verification_failed_rollback")
            self.surgeon.rollback_last()
            self.git._run("checkout", ".", check=False)
            self.journal.record_verification(journal_id, False)
            self.journal.finish_cycle(journal_id, "failed_verification")

        return "continue"

    def _fix_one_issue(self, issue: Issue, journal_id: int) -> bool:
        """Attempt to fix one issue. Returns True if patch applied."""
        log.info("fixing", issue=issue.description[:80],
                 files=issue.affected_files)

        # Read affected files
        file_contents = {}
        for f in issue.affected_files:
            # Resolve glob patterns like "skills/web_search/"
            if f.endswith("/"):
                for pyfile in self.surgeon.list_files(f + "**/*.py"):
                    content = self.surgeon.read_file(pyfile)
                    if content:
                        file_contents[pyfile] = content
            else:
                content = self.surgeon.read_file(f)
                if content:
                    file_contents[f] = content

        if not file_contents:
            log.warning("no_files_to_read", files=issue.affected_files)
            return False

        # Create fix branch
        slug = (
            issue.category + "-" +
            issue.description[:30]
            .lower()
            .replace(" ", "-")
            .replace("/", "-")
        )
        branch = self.git.create_branch(slug)

        # Plan fix with LLM
        plan = self.analyzer.plan_fix(issue, file_contents)

        if not plan:
            log.warning("no_fix_plan", issue=issue.description[:60])
            self.git.reset_branch(branch)
            return False

        confidence = plan.get("confidence", 0)
        if confidence < 0.4:
            log.warning("low_confidence_skip",
                        confidence=confidence,
                        issue=issue.description[:60])
            self.git.reset_branch(branch)
            return False

        # Apply patches
        results = self.surgeon.apply_plan(plan)
        all_ok = all(r.success for r in results)

        patched_files = [r.file for r in results if r.success]

        self.journal.record_patch(
            journal_id,
            issue.description,
            patched_files,
            all_ok,
        )

        if not all_ok:
            log.warning("patch_failed",
                        results=[str(r) for r in results])
            self.git.reset_branch(branch)
            return False

        # Commit on branch
        self.git.commit(
            f"fix({issue.category}): {issue.description[:60]}",
            files=patched_files,
        )

        # Merge to main
        merged = self.git.merge_to_main(branch)
        if not merged:
            self.git.reset_branch(branch)
            return False

        log.info("patch_applied_and_merged",
                 issue=issue.description[:60],
                 files=patched_files)
        return True

    def _verify_fixes(self, journal_id: int) -> bool:
        """Re-run tests after applying fixes."""
        # Rebuild (for Docker mode)
        self.runner.build()

        # Boot and run critical scenarios
        proc = self.runner.start()
        if not proc:
            return False

        comm = Communicator(proc, self.cfg)
        boot = comm.wait_for_boot(self.cfg.boot_timeout)

        if not boot.prompt_seen:
            log.error("verify_boot_failed")
            comm.close()
            self.runner.stop()
            return False

        # Run only critical + boot_health scenarios
        sr = ScenarioRunner(comm, self.cfg)
        results = sr.run_all(["boot_health", "echo_basic", "error_handling"])

        passed = all(r.passed for r in results)

        report = format_report(results)
        report_path = (
            self.cfg.logs_dir /
            f"cycle_{journal_id}_verify.txt"
        )
        report_path.write_text(report)
        print(report)

        comm.close()
        self.runner.stop()

        return passed

    def _shutdown(self):
        """Shut down running coreskill."""
        if self._comm:
            self._comm.close()
            self._comm = None
        self.runner.stop()
        self._proc = None


def main():
    parser = argparse.ArgumentParser(
        description="evo-supervisor: meta-evolution for coreskill"
    )
    parser.add_argument(
        "--coreskill-path", "-p",
        type=Path,
        default=Path("../coreskill"),
        help="Path to coreskill repository",
    )
    parser.add_argument(
        "--cycles", "-n",
        type=int,
        default=5,
        help="Maximum improvement cycles",
    )
    parser.add_argument(
        "--model", "-m",
        type=str,
        default=None,
        help="LLM model for analysis (default: claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--scenario", "-s",
        type=str,
        nargs="*",
        default=None,
        help="Run only specific scenarios",
    )
    parser.add_argument(
        "--test-only",
        action="store_true",
        help="Only run tests, don't apply fixes",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List available test scenarios",
    )

    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Use subprocess instead of Docker (faster, no build needed)",
    )
    args = parser.parse_args()

    if args.list_scenarios:
        print("Available scenarios:")
        for name, sc in SCENARIOS.items():
            critical = " [CRITICAL]" if sc.get("critical") else ""
            print(f"  {name:25s} {sc['description']}{critical}")
        return

    cfg = Config(
        coreskill_path=args.coreskill_path,
        max_cycles=args.cycles,
    )
    if args.model:
        cfg.supervisor_model = args.model

    # Choose runner: Docker or Subprocess
    runner_factory = SubprocessRunner if args.no_docker else create_runner

    errors = cfg.validate()
    if errors:
        for e in errors:
            print(f"CONFIG ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.test_only:
        # Test-only mode: boot, test, report, exit
        runner = runner_factory(cfg)
        if not runner.build():
            sys.exit(1)

        proc = runner.start()
        if not proc:
            sys.exit(1)

        comm = Communicator(proc, cfg)
        boot = comm.wait_for_boot(cfg.boot_timeout)

        if not boot.prompt_seen:
            print("Boot failed!")
            print(boot.raw[-500:])
            sys.exit(1)

        sr = ScenarioRunner(comm, cfg)
        names = args.scenario if args.scenario else None
        results = sr.run_all(names)
        print(format_report(results))

        comm.close()
        runner.stop()

        ok = all(r.passed for r in results)
        sys.exit(0 if ok else 1)

    # Full supervisor mode
    supervisor = Supervisor(cfg, runner_factory=runner_factory)
    supervisor.run(max_cycles=args.cycles)


if __name__ == "__main__":
    main()
