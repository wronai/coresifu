"""Manages coreskill Docker container lifecycle."""
import subprocess
import shutil
import time
import json
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()


class DockerRunner:
    """Build, start, stop coreskill Docker containers."""

    def __init__(self, config):
        self.cfg = config
        self.container_id: Optional[str] = None
        self._docker = shutil.which("docker")

    def available(self) -> bool:
        """Check if Docker is available."""
        if not self._docker:
            return False
        try:
            r = subprocess.run(
                ["docker", "info"],
                capture_output=True, timeout=10
            )
            return r.returncode == 0
        except Exception:
            return False

    def build(self) -> bool:
        """Build Docker image from coreskill source."""
        dockerfile = Path(__file__).parent.parent / "Dockerfile.coreskill"

        if not dockerfile.exists():
            log.error("dockerfile_missing", path=str(dockerfile))
            return False

        log.info("docker_build_start", image=self.cfg.docker_image)

        try:
            r = subprocess.run(
                [
                    "docker", "build",
                    "-f", str(dockerfile),
                    "-t", self.cfg.docker_image,
                    str(self.cfg.coreskill_path),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )

            if r.returncode != 0:
                log.error("docker_build_failed", stderr=r.stderr[-500:])
                return False

            log.info("docker_build_ok", image=self.cfg.docker_image)
            return True

        except subprocess.TimeoutExpired:
            log.error("docker_build_timeout")
            return False

    def start(self) -> Optional[subprocess.Popen]:
        """Start coreskill container, return Popen with stdin/stdout pipes.

        The container mounts coreskill_path as /app so code changes
        on host are visible inside (no rebuild needed for source changes).
        """
        env_args = []
        if self.cfg.api_key:
            env_args += ["-e", f"OPENROUTER_API_KEY={self.cfg.api_key}"]

        cmd = [
            "docker", "run",
            "--rm",
            "-i",                    # interactive stdin
            "--name", "evo-coreskill-test",
            "-v", f"{self.cfg.coreskill_path}:/app:ro",  # mount source read-only
            "-e", "EVO_TEXT_ONLY=1",
            "-e", "PYTHONUNBUFFERED=1",
            "-e", "TERM=dumb",
            *env_args,
            self.cfg.docker_image,
            "python3", "main.py", "--no-voice",
        ]

        log.info("docker_start", cmd=" ".join(cmd[:8]))

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line buffered
            )
            return proc

        except Exception as e:
            log.error("docker_start_failed", error=str(e))
            return None

    def stop(self):
        """Stop running container."""
        try:
            subprocess.run(
                ["docker", "stop", "evo-coreskill-test"],
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                ["docker", "rm", "-f", "evo-coreskill-test"],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass

    def cleanup(self):
        """Force-remove container if stuck."""
        self.stop()


class SubprocessRunner:
    """Run coreskill directly as subprocess (no Docker)."""

    def __init__(self, config):
        self.cfg = config

    def available(self) -> bool:
        return (self.cfg.coreskill_path / "main.py").exists()

    def build(self) -> bool:
        """No build needed for subprocess mode."""
        return True

    def start(self) -> Optional[subprocess.Popen]:
        """Start coreskill as direct subprocess."""
        import os

        env = os.environ.copy()
        env["EVO_TEXT_ONLY"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["TERM"] = "dumb"
        if self.cfg.api_key:
            env["OPENROUTER_API_KEY"] = self.cfg.api_key

        cmd = ["python3", "main.py", "--no-voice"]

        log.info("subprocess_start", cwd=str(self.cfg.coreskill_path))

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(self.cfg.coreskill_path),
                env=env,
            )
            return proc

        except Exception as e:
            log.error("subprocess_start_failed", error=str(e))
            return None

    def stop(self):
        pass

    def cleanup(self):
        pass


def create_runner(config):
    """Factory: Docker if available, else subprocess."""
    docker = DockerRunner(config)
    if docker.available():
        log.info("using_docker")
        return docker

    log.info("docker_unavailable_using_subprocess")
    return SubprocessRunner(config)
