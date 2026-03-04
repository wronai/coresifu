"""evo-supervisor configuration."""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    """All supervisor settings."""

    # Paths
    coreskill_path: Path = field(default_factory=lambda: Path("../coreskill"))
    workspace: Path = field(default_factory=lambda: Path("./workspace"))
    logs_dir: Path = field(default_factory=lambda: Path("./logs"))
    patches_dir: Path = field(default_factory=lambda: Path("./patches"))

    # Docker
    docker_image: str = "evo-coreskill:latest"
    docker_timeout: int = 120  # seconds for container boot

    # LLM for supervisor reasoning
    supervisor_model: str = "openrouter/moonshotai/kimi-k2.5"
    fallback_model: str = "openrouter/meta-llama/llama-3.3-70b-instruct:free"
    api_key: str = ""

    # Improvement loop
    max_cycles: int = 10
    max_patches_per_cycle: int = 3
    boot_timeout: int = 60  # seconds to wait for "you> " prompt
    command_timeout: int = 30  # seconds per command response
    verify_retries: int = 2

    # Safety
    max_file_changes_per_patch: int = 5
    forbidden_paths: list = field(default_factory=lambda: [
        "main.py",  # don't touch entry point initially
        ".git/",
        "__pycache__/",
        ".evo_state.json",
    ])
    require_tests_pass: bool = True
    git_branch_prefix: str = "supervisor/"

    def __post_init__(self):
        self.coreskill_path = Path(self.coreskill_path).resolve()
        self.workspace = Path(self.workspace).resolve()
        self.logs_dir = Path(self.logs_dir).resolve()
        self.patches_dir = Path(self.patches_dir).resolve()

        self.api_key = self.api_key or os.environ.get("OPENROUTER_API_KEY", "")

        # Create dirs
        for d in [self.workspace, self.logs_dir, self.patches_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        """Return list of config errors."""
        errors = []
        if not self.coreskill_path.exists():
            errors.append(f"coreskill_path not found: {self.coreskill_path}")
        if not self.api_key:
            errors.append("No OPENROUTER_API_KEY set")
        if not (self.coreskill_path / "main.py").exists():
            errors.append(f"No main.py in {self.coreskill_path}")
        return errors
