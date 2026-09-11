from pathlib import Path
import os
import subprocess
import sys
from time import monotonic

import pytest


def test_outer_deadline_covers_stuck_shutdown_and_cleans_child_and_media(tmp_path):
    from smoke_support import run_bounded_smoke, SmokeTimeout
    child_pid_file = tmp_path / "child.pid"
    script = """
import pathlib, subprocess, sys, time
workspace = pathlib.Path(sys.argv[1])
(workspace / 'temporary.m4a').write_bytes(b'temporary')
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path(sys.argv[2]).write_text(str(child.pid))
# Model an executor shutdown that never returns; the outer process owns timeout.
time.sleep(60)
"""
    started = monotonic()
    with pytest.raises(SmokeTimeout):
        run_bounded_smoke([sys.executable, "-c", script], tmp_path,
                          trailing_args=[str(child_pid_file)], timeout=.5)
    assert monotonic() - started < 3
    assert not list(tmp_path.glob("smoke-*"))
    child_pid = int(child_pid_file.read_text())
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True, timeout=1)
    assert not status.stdout.strip() or status.stdout.strip().startswith("Z"), "Smoke child is still running"
