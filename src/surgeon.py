"""Code surgeon — applies patches to coreskill source with safety."""
import re
import shutil
import difflib
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()


class PatchResult:
    """Result of applying a single patch."""

    def __init__(self, file: str, success: bool, detail: str = ""):
        self.file = file
        self.success = success
        self.detail = detail

    def __repr__(self):
        status = "OK" if self.success else "FAIL"
        return f"[{status}] {self.file}: {self.detail}"


class Surgeon:
    """Applies code patches to coreskill source.

    Safety rules:
    - Always creates backup before modifying
    - Verifies file exists and search string found
    - Validates Python syntax after patching
    - Rolls back on syntax error
    - Refuses to touch forbidden paths
    """

    def __init__(self, config):
        self.cfg = config
        self.root = config.coreskill_path
        self.backup_dir = config.workspace / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._applied = []  # track for rollback

    def apply_plan(self, plan: dict) -> list[PatchResult]:
        """Apply a full fix plan (from Analyzer.plan_fix).

        Args:
            plan: {"patches": [...], "confidence": 0.8, ...}

        Returns:
            List of PatchResult for each patch.
        """
        self._applied = []  # reset per-plan so rollback_last() stays scoped
        patches = plan.get("patches", [])
        confidence = plan.get("confidence", 0)

        if confidence < 0.3:
            log.warning("low_confidence_skip", confidence=confidence)
            return [PatchResult("*", False, f"Confidence too low: {confidence}")]

        if len(patches) > self.cfg.max_file_changes_per_patch:
            log.warning("too_many_patches", count=len(patches))
            return [PatchResult("*", False,
                                f"Too many patches: {len(patches)} > "
                                f"{self.cfg.max_file_changes_per_patch}")]

        results = []
        applied_files = []

        for patch in patches:
            result = self._apply_one(patch)
            results.append(result)

            if result.success:
                applied_files.append(result.file)
            else:
                # Rollback all patches from this plan
                log.error("patch_failed_rollback",
                          file=result.file, detail=result.detail)
                self._rollback_files(applied_files)
                for r in results:
                    if r.success:
                        r.success = False
                        r.detail += " (rolled back)"
                break

        return results

    def _apply_one(self, patch: dict) -> PatchResult:
        """Apply a single patch to one file."""
        file_rel = patch.get("file", "")
        action = patch.get("action", "replace")
        search = patch.get("search", "")
        replace_str = patch.get("replace", "")
        content = patch.get("content", "")

        # Safety: check forbidden paths
        for forbidden in self.cfg.forbidden_paths:
            if forbidden in file_rel:
                return PatchResult(file_rel, False,
                                   f"Forbidden path: {forbidden}")

        file_path = self.root / file_rel
        if not file_path.exists():
            return PatchResult(file_rel, False, "File not found")

        if not file_path.is_file():
            return PatchResult(file_rel, False, "Not a file")

        # Read original
        try:
            original = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return PatchResult(file_rel, False, f"Read error: {e}")

        # Create backup
        self._backup(file_path, original)

        # Apply patch
        if action == "replace":
            if not search:
                return PatchResult(file_rel, False, "Empty search string")

            count = original.count(search)
            if count == 0:
                # Try fuzzy match
                fuzzy_result = self._fuzzy_find(original, search)
                if fuzzy_result:
                    log.info("fuzzy_match_used", file=file_rel,
                             ratio=fuzzy_result[1])
                    new_content = original.replace(
                        fuzzy_result[0], replace_str, 1
                    )
                else:
                    return PatchResult(file_rel, False,
                                       "Search string not found (even fuzzy)")
            elif count > 1:
                # Replace only first occurrence
                log.warning("multiple_matches", file=file_rel, count=count)
                new_content = original.replace(search, replace_str, 1)
            else:
                new_content = original.replace(search, replace_str, 1)

        elif action == "insert_after":
            if search not in original:
                return PatchResult(file_rel, False,
                                   f"Anchor not found: {search[:50]}")
            idx = original.index(search) + len(search)
            new_content = original[:idx] + "\n" + content + original[idx:]

        elif action == "insert_before":
            if search not in original:
                return PatchResult(file_rel, False,
                                   f"Anchor not found: {search[:50]}")
            idx = original.index(search)
            new_content = original[:idx] + content + "\n" + original[idx:]

        elif action == "delete":
            if search not in original:
                return PatchResult(file_rel, False,
                                   f"Delete target not found: {search[:50]}")
            new_content = original.replace(search, "", 1)

        else:
            return PatchResult(file_rel, False, f"Unknown action: {action}")

        # Validate Python syntax (if .py file)
        if file_rel.endswith(".py"):
            syntax_ok, syntax_err = self._check_syntax(new_content)
            if not syntax_ok:
                self._restore(file_path)
                return PatchResult(file_rel, False,
                                   f"Syntax error after patch: {syntax_err}")

        # Write
        try:
            file_path.write_text(new_content, encoding="utf-8")
        except Exception as e:
            self._restore(file_path)
            return PatchResult(file_rel, False, f"Write error: {e}")

        # Generate diff for logging
        diff = self._make_diff(original, new_content, file_rel)
        self._save_diff(file_rel, diff)

        self._applied.append((file_path, original))

        lines_changed = sum(
            1 for line in diff.splitlines()
            if line.startswith("+") or line.startswith("-")
        )

        log.info("patch_applied", file=file_rel, action=action,
                 lines_changed=lines_changed)

        return PatchResult(file_rel, True,
                           f"{action}: {lines_changed} lines changed")

    def _backup(self, file_path: Path, content: str):
        """Create timestamped backup."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        name_hash = hashlib.md5(
            str(file_path).encode()
        ).hexdigest()[:8]
        backup = self.backup_dir / f"{ts}_{name_hash}_{file_path.name}"
        backup.write_text(content, encoding="utf-8")

    def _restore(self, file_path: Path):
        """Restore from most recent backup."""
        name_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
        backups = sorted(
            self.backup_dir.glob(f"*_{name_hash}_{file_path.name}"),
            reverse=True,
        )
        if backups:
            shutil.copy2(str(backups[0]), str(file_path))
            log.info("file_restored", file=str(file_path.name))

    def _rollback_files(self, file_rels: list[str]):
        """Rollback all files from a failed plan."""
        for file_rel in file_rels:
            file_path = self.root / file_rel
            self._restore(file_path)
        log.info("rollback_complete", files=len(file_rels))

    def rollback_last(self) -> bool:
        """Rollback the last applied set of patches."""
        if not self._applied:
            return False
        for file_path, original_content in reversed(self._applied):
            file_path.write_text(original_content, encoding="utf-8")
        count = len(self._applied)
        self._applied.clear()
        log.info("rollback_last_ok", files=count)
        return True

    def _check_syntax(self, code: str) -> tuple[bool, str]:
        """Check Python syntax validity."""
        try:
            compile(code, "<patch>", "exec")
            return True, ""
        except SyntaxError as e:
            return False, f"Line {e.lineno}: {e.msg}"

    def _fuzzy_find(self, text: str, search: str,
                     threshold: float = 0.7) -> Optional[tuple[str, float]]:
        """Try to find approximate match for search string.

        Useful when LLM gets whitespace or minor chars wrong.
        """
        search_lines = search.strip().splitlines()
        text_lines = text.splitlines()

        if len(search_lines) < 1:
            return None

        # Sliding window
        window = len(search_lines)
        best_ratio = 0.0
        best_match = None

        for i in range(len(text_lines) - window + 1):
            candidate = "\n".join(text_lines[i:i + window])
            ratio = difflib.SequenceMatcher(
                None, search.strip(), candidate.strip()
            ).ratio()

            if ratio > best_ratio:
                best_ratio = ratio
                best_match = candidate

        if best_ratio >= threshold and best_match:
            return best_match, best_ratio
        return None

    def _make_diff(self, old: str, new: str, filename: str) -> str:
        """Generate unified diff."""
        return "".join(difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        ))

    def _save_diff(self, file_rel: str, diff: str):
        """Save diff to patches directory."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_name = file_rel.replace("/", "_").replace("\\", "_")
        diff_path = self.cfg.patches_dir / f"{ts}_{safe_name}.patch"
        diff_path.write_text(diff, encoding="utf-8")

    def read_file(self, file_rel: str) -> Optional[str]:
        """Read a file from coreskill source."""
        path = self.root / file_rel
        if path.exists() and path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def list_files(self, pattern: str = "**/*.py") -> list[str]:
        """List files in coreskill matching pattern."""
        results = []
        for p in self.root.glob(pattern):
            if "__pycache__" in str(p) or ".git" in str(p):
                continue
            results.append(str(p.relative_to(self.root)))
        return sorted(results)
