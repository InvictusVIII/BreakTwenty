from __future__ import annotations

import hashlib
import os
import re
import subprocess  # nosec B404
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_HEARTBEAT_SECONDS = 30.0
DEFAULT_FAILURE_TAIL_LINES = 120
DEFAULT_FAILURE_TAIL_BYTES = 128 * 1024


@dataclass(frozen=True)
class QuietCommandResult:
    exit_code: int | None
    duration_ms: int
    output_bytes: int
    output_sha256: str
    retained_log: str | None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and self.error is None

    def receipt_output(self) -> dict[str, object]:
        return {
            "status": "passed" if self.passed else "failed",
            "bytes": self.output_bytes,
            "sha256": self.output_sha256,
            "retained": self.retained_log is not None,
            "log_path": self.retained_log,
        }


def _safe_label(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.").lower()
    return normalized or "validation-command"


def _file_fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _failure_tail(
    path: Path,
    *,
    max_lines: int,
    max_bytes: int,
) -> str:
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size > max_bytes:
            stream.seek(size - max_bytes)
        payload = stream.read()
    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines()
    selected = lines[-max(max_lines, 1):]
    omitted = size > len(payload) or len(lines) > len(selected)
    prefix = "[earlier command output omitted]\n" if omitted else ""
    return prefix + "\n".join(selected)


def _receipt_log_path(path: Path, receipt_root: Path) -> str:
    try:
        return path.resolve().relative_to(receipt_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_quiet_command(
    command: Sequence[str],
    *,
    cwd: Path,
    label: str,
    log_directory: Path,
    receipt_root: Path,
    env: dict[str, str] | None = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    failure_tail_lines: int = DEFAULT_FAILURE_TAIL_LINES,
    failure_tail_bytes: int = DEFAULT_FAILURE_TAIL_BYTES,
    printer: Callable[..., None] = print,
) -> QuietCommandResult:
    """Run a validation command quietly while retaining complete output only on failure."""

    started = time.monotonic()
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        log_directory.chmod(0o700)
    except OSError:
        pass
    temporary = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix=f".{_safe_label(label)}-running-",
        suffix=".log",
        dir=log_directory,
        delete=False,
    )
    temporary_path = Path(temporary.name)
    process: subprocess.Popen[bytes] | None = None
    exit_code: int | None = None
    error_message: str | None = None
    interrupted: BaseException | None = None
    try:
        try:
            process = subprocess.Popen(  # nosec B603
                list(command),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=temporary,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            temporary.write(f"{error_message}\n".encode("utf-8", errors="replace"))
        if process is not None:
            next_heartbeat = time.monotonic() + heartbeat_seconds
            while exit_code is None:
                wait_seconds = max(next_heartbeat - time.monotonic(), 0.05)
                try:
                    exit_code = process.wait(timeout=wait_seconds)
                except subprocess.TimeoutExpired:
                    elapsed_seconds = round(time.monotonic() - started)
                    printer(f"    still running: {label} ({elapsed_seconds}s elapsed)", flush=True)
                    next_heartbeat = time.monotonic() + heartbeat_seconds
    except BaseException as exc:
        if process is not None:
            _stop_process(process)
            exit_code = process.poll()
        interrupted = exc
        error_message = f"{type(exc).__name__}: {exc}"
    finally:
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary.close()

    duration_ms = round((time.monotonic() - started) * 1000)
    output_bytes, output_sha256 = _file_fingerprint(temporary_path)
    passed = exit_code == 0 and error_message is None
    retained_log: str | None = None
    if passed:
        temporary_path.unlink(missing_ok=True)
        printer(
            f"    passed: {label} ({duration_ms / 1000:.1f}s; "
            f"{output_bytes} output bytes captured and discarded)",
            flush=True,
        )
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        failure_path = log_directory / (
            f"{_safe_label(label)}-{timestamp}-{os.getpid()}.log"
        )
        os.replace(temporary_path, failure_path)
        retained_log = _receipt_log_path(failure_path, receipt_root)
        tail = _failure_tail(
            failure_path,
            max_lines=failure_tail_lines,
            max_bytes=failure_tail_bytes,
        )
        printer(
            f"    failed: {label} (exit={exit_code}; {duration_ms / 1000:.1f}s)\n"
            f"    complete output: {retained_log}\n"
            "--- failure output tail ---\n"
            f"{tail}\n"
            "--- end failure output tail ---",
            flush=True,
        )

    result = QuietCommandResult(
        exit_code=exit_code,
        duration_ms=duration_ms,
        output_bytes=output_bytes,
        output_sha256=output_sha256,
        retained_log=retained_log,
        error=error_message,
    )
    if interrupted is not None:
        raise interrupted
    return result
