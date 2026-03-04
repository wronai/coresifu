"""Communicates with coreskill process via stdin/stdout."""
import re
import time
import select
import threading
from queue import Queue, Empty
from dataclasses import dataclass, field
from typing import Optional

import structlog

log = structlog.get_logger()


@dataclass
class Response:
    """Single response from coreskill."""
    raw: str
    command: str = ""
    duration_s: float = 0.0
    has_error: bool = False
    has_warning: bool = False
    health_issues: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    prompt_seen: bool = False


class Communicator:
    """Send commands to coreskill and read responses.

    Uses a background reader thread to handle stdout non-blockingly.
    """

    PROMPT_PATTERN = re.compile(r"(?:\x1b\[\d+m)?(?:you|evo)>\s*(?:\x1b\[0m)?$", re.MULTILINE)
    ERROR_PATTERNS = [
        re.compile(r"\[(?:ERROR|FAIL|CRASH)\](.+)", re.IGNORECASE),
        re.compile(r"Traceback \(most recent call last\)"),
        re.compile(r"(?:Error|Exception|FAIL):\s*(.+)"),
        re.compile(r"\[REPAIR\]\s*✗\s*(.+)"),
        re.compile(r"\[HEALTH\]\s*⚠\s*(.+)"),
        re.compile(r"Nadal uszkodzone:\s*(.+)"),
    ]
    WARNING_PATTERNS = [
        re.compile(r"\[WARN(?:ING)?\](.+)", re.IGNORECASE),
        re.compile(r"⚠\s*(.+)"),
        re.compile(r"\[REFLECT\]\s*✗\s*(.+)"),
    ]

    def __init__(self, proc, config):
        self.proc = proc
        self.cfg = config
        self._queue = Queue()
        self._buffer = ""
        self._all_output = []
        self._last_prompt_seen = True  # assume clean state after boot

        # Start background reader
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True
        )
        self._reader.start()

    def _read_loop(self):
        """Background: read chars from stdout into queue (handles prompts without newlines)."""
        try:
            buf = ""
            while True:
                ch = self.proc.stdout.read(1)
                if not ch:
                    if buf:
                        self._queue.put(buf)
                    self._queue.put(None)  # EOF
                    break
                buf += ch
                # Flush buffer on newline OR when we see a prompt pattern
                if ch == "\n" or self.PROMPT_PATTERN.search(buf):
                    self._queue.put(buf)
                    buf = ""
        except Exception as e:
            log.error("read_loop_exception", error=str(e), error_type=type(e).__name__)
            self._queue.put(None)

    def wait_for_boot(self, timeout: int = 60) -> Response:
        """Wait for coreskill to boot and show first prompt."""
        log.info("waiting_for_boot", timeout=timeout)
        # Send a newline to unblock the first input() call in coreskill
        # Wait a bit first for coreskill to initialize (needs time for full boot)
        return self._read_until_prompt(timeout, command="<boot>")

    def _resync(self, wait: int = 10):
        """Wait for you> prompt after a timed-out command before sending next one."""
        log.debug("resyncing_after_timeout", wait=wait)
        start = time.time()
        while time.time() - start < wait:
            try:
                item = self._queue.get(timeout=0.5)
            except Empty:
                continue
            if item is None:
                return
            self._buffer += item
            if self.PROMPT_PATTERN.search(self._buffer):
                self._buffer = ""
                log.debug("resync_ok")
                return
        log.warning("resync_timeout", wait=wait)
        self._buffer = ""

    def send(self, command: str, timeout: int = 30) -> Response:
        """Send a command and wait for response."""
        log.info("send_command", command=command[:80])
        if not self._last_prompt_seen:
            self._resync(wait=15)

        try:
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            log.error("stdin_broken", error=str(e))
            return Response(
                raw="", command=command, has_error=True,
                errors=[f"Process stdin broken: {e}"]
            )

        resp = self._read_until_prompt(timeout, command=command)
        self._last_prompt_seen = resp.prompt_seen
        return resp

    def _read_until_prompt(self, timeout: int, command: str = "") -> Response:
        """Read output until we see the you> prompt or timeout."""
        lines = []
        start = time.time()

        while time.time() - start < timeout:
            try:
                line = self._queue.get(timeout=0.5)
            except Empty:
                continue

            if line is None:
                log.info("process_exited_or_reader_error", returncode=self.proc.poll(), command=command[:50])
                break

            log.debug("queue_item", item=repr(line[:80]), command=command[:30])

            lines.append(line)
            self._all_output.append(line)
            self._buffer += line

            # Check for prompt
            if self.PROMPT_PATTERN.search(self._buffer):
                self._buffer = ""
                break
        else:
            log.warning("read_timeout", command=command[:50], timeout=timeout)
            log.debug("buffer_content", buffer=self._buffer[-500:])

        raw = "".join(lines)
        duration = time.time() - start

        resp = Response(
            raw=raw,
            command=command,
            duration_s=round(duration, 2),
            prompt_seen=bool(self.PROMPT_PATTERN.search(raw)),
        )

        # Detect errors
        for pattern in self.ERROR_PATTERNS:
            for match in pattern.finditer(raw):
                resp.has_error = True
                resp.errors.append(match.group(0).strip())

        for pattern in self.WARNING_PATTERNS:
            for match in pattern.finditer(raw):
                resp.has_warning = True

        # Detect health issues
        for line in raw.splitlines():
            if "[HEALTH] ⚠" in line:
                resp.health_issues.append(line.strip())

        return resp

    def send_and_expect(self, command: str, expect: str,
                         timeout: int = 30) -> tuple[bool, Response]:
        """Send command, check if expected string appears in response."""
        resp = self.send(command, timeout)
        found = expect.lower() in resp.raw.lower()
        return found, resp

    def is_alive(self) -> bool:
        """Check if the process is still running."""
        if self.proc is None:
            return False
        return self.proc.poll() is None

    def get_full_log(self) -> str:
        """Return all output captured so far."""
        return "".join(self._all_output)

    def close(self):
        """Terminate the process."""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write("/exit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
