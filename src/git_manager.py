"""Git operations on coreskill repository."""
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()


class GitManager:
    """Manages git operations on coreskill repo.

    Workflow:
    1. Create branch: supervisor/fix-{issue}
    2. Apply patches (via Surgeon)
    3. Commit with descriptive message
    4. If verify passes → merge to main
    5. If verify fails → reset branch
    """

    def __init__(self, config):
        self.cfg = config
        self.root = config.coreskill_path

    def _run(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        """Run git command in coreskill root."""
        cmd = ["git"] + list(args)
        return subprocess.run(
            cmd,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=30,
            check=check,
        )

    def is_repo(self) -> bool:
        """Check if coreskill is a git repo."""
        try:
            r = self._run("rev-parse", "--is-inside-work-tree", check=False)
            return r.returncode == 0
        except Exception:
            return False

    def init_if_needed(self):
        """Initialize git repo if not present."""
        if not self.is_repo():
            self._run("init")
            self._run("add", "-A")
            self._run("commit", "-m", "Initial commit (evo-supervisor)")
            log.info("git_init_done")

    def current_branch(self) -> str:
        """Get current branch name."""
        r = self._run("branch", "--show-current", check=False)
        return r.stdout.strip() or "main"

    def is_clean(self) -> bool:
        """Check if working directory is clean."""
        r = self._run("status", "--porcelain", check=False)
        return r.stdout.strip() == ""

    def stash_if_dirty(self) -> bool:
        """Stash uncommitted changes. Returns True if stashed."""
        if not self.is_clean():
            self._run("stash", "push", "-m", "evo-supervisor auto-stash")
            log.info("git_stashed")
            return True
        return False

    def stash_pop(self):
        """Pop stashed changes."""
        try:
            self._run("stash", "pop", check=False)
        except Exception:
            pass

    def create_branch(self, issue_slug: str) -> str:
        """Create and checkout a fix branch."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        branch = f"{self.cfg.git_branch_prefix}{issue_slug}-{ts}"
        # Max 60 chars
        branch = branch[:60]

        self._run("checkout", "-b", branch)
        log.info("branch_created", branch=branch)
        return branch

    def commit(self, message: str, files: Optional[list[str]] = None):
        """Commit changes with message.

        Args:
            message: Commit message
            files: Specific files to add, or None for all changes
        """
        if files:
            for f in files:
                self._run("add", f, check=False)
        else:
            self._run("add", "-A")

        r = self._run("commit", "-m", message, check=False)
        if r.returncode == 0:
            log.info("git_commit", message=message[:80])
        else:
            log.warning("git_commit_nothing", stderr=r.stderr[:100])

    def merge_to_main(self, branch: str) -> bool:
        """Merge fix branch back to main."""
        main_branch = self._detect_main_branch()

        try:
            self._run("checkout", main_branch)
            self._run("merge", branch, "--no-ff",
                       "-m", f"Merge {branch} (evo-supervisor verified)")
            log.info("git_merged", branch=branch, target=main_branch)
            # Clean up branch
            self._run("branch", "-d", branch, check=False)
            return True
        except subprocess.CalledProcessError as e:
            log.error("git_merge_failed", branch=branch, error=e.stderr[:200])
            # Abort merge if conflicted
            self._run("merge", "--abort", check=False)
            self._run("checkout", branch, check=False)
            return False

    def reset_branch(self, branch: str):
        """Abandon a fix branch and return to main."""
        main_branch = self._detect_main_branch()
        self._run("checkout", main_branch, check=False)
        self._run("branch", "-D", branch, check=False)
        log.info("branch_abandoned", branch=branch)

    def tag(self, name: str, message: str = ""):
        """Create an annotated tag."""
        args = ["tag", "-a", name, "-m", message or name]
        self._run(*args, check=False)

    def log_recent(self, n: int = 10) -> list[dict]:
        """Get recent commits."""
        fmt = "%H|%s|%ai"
        r = self._run("log", f"-{n}", f"--format={fmt}", check=False)
        commits = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                commits.append({
                    "hash": parts[0][:8],
                    "message": parts[1],
                    "date": parts[2],
                })
        return commits

    def diff_stat(self) -> str:
        """Get diff --stat of uncommitted changes."""
        r = self._run("diff", "--stat", check=False)
        return r.stdout

    def _detect_main_branch(self) -> str:
        """Detect main branch name (main or master)."""
        r = self._run("branch", "--list", "main", check=False)
        if "main" in r.stdout:
            return "main"
        r = self._run("branch", "--list", "master", check=False)
        if "master" in r.stdout:
            return "master"
        return "main"
