"""The supervisor's kill-on-exit job: killing the supervisor also kills the server it started (Windows only)."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-only")

PARENT = """
import subprocess, sys, time
from pmjc import service
assert service._die_with_children() is not None
c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(c.pid, flush=True)
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def test_killing_supervisor_kills_its_children():
    parent = subprocess.Popen([sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True)
    try:
        child = int(parent.stdout.readline())
        assert _alive(child)
        parent.kill()  # what schtasks /End does to the supervisor
        parent.wait(10)
        for _ in range(50):
            if not _alive(child):
                break
            time.sleep(0.1)
        assert not _alive(child)
    finally:
        parent.kill()
