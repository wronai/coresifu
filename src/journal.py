"""Persistent journal of all supervisor improvement cycles."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()


class Journal:
    """Records every action: what was tested, found, patched, verified.

    Enables:
    - Don't repeat failed fixes
    - Track progress over time
    - Know which issues keep recurring
    """

    def __init__(self, path: Path):
        self.path = path / "journal.json"
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                return self._empty()
        return self._empty()

    def _empty(self) -> dict:
        return {
            "cycles": [],
            "known_issues": {},
            "fixed_issues": [],
            "failed_fixes": [],
            "stats": {
                "total_cycles": 0,
                "total_patches": 0,
                "successful_patches": 0,
                "failed_patches": 0,
            },
        }

    def _save(self):
        self.path.write_text(json.dumps(self.data, indent=2, default=str))

    def start_cycle(self) -> int:
        """Start new improvement cycle. Returns cycle number."""
        cycle_num = self.data["stats"]["total_cycles"] + 1
        self.data["stats"]["total_cycles"] = cycle_num
        self.data["cycles"].append({
            "number": cycle_num,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "issues_found": [],
            "patches_applied": [],
            "verified": None,
            "outcome": "in_progress",
        })
        self._save()
        return cycle_num

    def record_issues(self, cycle_num: int, issues: list[dict]):
        """Record issues found in a cycle."""
        cycle = self._get_cycle(cycle_num)
        if cycle:
            cycle["issues_found"] = [
                {"severity": i.get("severity"), "description": i.get("description"),
                 "category": i.get("category")}
                for i in issues
            ]
            self._save()

    def record_patch(self, cycle_num: int, issue_desc: str,
                      files: list[str], success: bool):
        """Record a patch attempt."""
        cycle = self._get_cycle(cycle_num)
        if cycle:
            cycle["patches_applied"].append({
                "issue": issue_desc[:100],
                "files": files,
                "success": success,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        self.data["stats"]["total_patches"] += 1
        if success:
            self.data["stats"]["successful_patches"] += 1
        else:
            self.data["stats"]["failed_patches"] += 1
        self._save()

    def record_verification(self, cycle_num: int, passed: bool,
                             detail: str = ""):
        """Record verification result after patching."""
        cycle = self._get_cycle(cycle_num)
        if cycle:
            cycle["verified"] = passed
            cycle["outcome"] = "success" if passed else "failed_verification"
            cycle["finished_at"] = datetime.now(timezone.utc).isoformat()
            cycle["verification_detail"] = detail
        self._save()

    def finish_cycle(self, cycle_num: int, outcome: str):
        """Mark cycle as finished."""
        cycle = self._get_cycle(cycle_num)
        if cycle:
            cycle["finished_at"] = datetime.now(timezone.utc).isoformat()
            cycle["outcome"] = outcome
        self._save()

    def was_tried(self, issue_desc: str) -> Optional[dict]:
        """Check if this issue was already attempted.

        Returns the attempt record or None.
        """
        desc_lower = issue_desc.lower().strip()
        for cycle in self.data["cycles"]:
            for patch in cycle.get("patches_applied", []):
                prior_lower = patch["issue"].lower().strip()
                # Require both strings to be meaningful length before substring
                # matching to avoid "crash" matching every attempt
                if len(desc_lower) >= 20 and len(prior_lower) >= 20:
                    if prior_lower in desc_lower or desc_lower in prior_lower:
                        return {
                            "cycle": cycle["number"],
                            "success": patch["success"],
                            "verified": cycle.get("verified"),
                        }
                elif desc_lower == prior_lower:
                    return {
                        "cycle": cycle["number"],
                        "success": patch["success"],
                        "verified": cycle.get("verified"),
                    }
        return None

    def recurring_issues(self) -> list[dict]:
        """Find issues that keep appearing across cycles."""
        desc_count = {}
        for cycle in self.data["cycles"]:
            for issue in cycle.get("issues_found", []):
                key = issue.get("description", "")[:60].lower()
                if key:
                    desc_count[key] = desc_count.get(key, 0) + 1

        return [
            {"description": k, "occurrences": v}
            for k, v in desc_count.items()
            if v >= 2
        ]

    def summary(self) -> str:
        """Human-readable summary."""
        s = self.data["stats"]
        lines = [
            f"Cycles: {s['total_cycles']}",
            f"Patches: {s['total_patches']} "
            f"(ok: {s['successful_patches']}, "
            f"fail: {s['failed_patches']})",
        ]

        recurring = self.recurring_issues()
        if recurring:
            lines.append(f"Recurring issues: {len(recurring)}")
            for ri in recurring[:5]:
                lines.append(f"  - {ri['description']} (×{ri['occurrences']})")

        return "\n".join(lines)

    def _get_cycle(self, num: int) -> Optional[dict]:
        for c in self.data["cycles"]:
            if c["number"] == num:
                return c
        return None
