"""Test-only supervisor: bound the complete smoke process and its descendants."""

import os
from pathlib import Path
import signal
import subprocess
import tempfile


class SmokeTimeout(RuntimeError):
    pass


def run_bounded_smoke(command: list[str], root: Path, *, trailing_args=(), timeout=12) -> None:
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=root) as workspace:
        process = subprocess.Popen([*command, workspace, *trailing_args], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            process.wait(timeout=timeout)
            if process.returncode:
                raise AssertionError("Offline smoke subprocess failed; no provider diagnostics retained")
        except subprocess.TimeoutExpired:
            raise SmokeTimeout("Offline smoke exceeded the complete-process deadline") from None
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
    # The outer supervisor owns cleanup even if the inner worker never returns.
