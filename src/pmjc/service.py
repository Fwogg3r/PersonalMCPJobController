"""Always-on service: start at logon/boot, restart on crash, one instance, keep the PC awake.

Windows: a per-user scheduled task at logon (must be the interactive session to read notifications and drive
the browser) that runs `pythonw -m pmjc service run` (no console window). The supervisor restarts the server
if it exits. After a reboot it starts when you sign in; use Windows auto sign-in if the PC must recover alone.
Linux: systemd user unit (run `loginctl enable-linger $USER` so it survives logout). macOS: launchd agent.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config

TASK = "pmjc"
log = logging.getLogger("pmjc.service")


def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def setup_logging() -> Path:
    f = config.path("logs", "pmjc.log")
    h = logging.handlers.RotatingFileHandler(f, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    from .creds import Redactor
    h.addFilter(Redactor())
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can carry keys
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # every poll would be logged
    return f


def _task_xml() -> str:
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    cwd = str(Path(__file__).resolve().parents[2])
    home = str(config.home())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>pmjc switchboard (MCP, REST, jobs)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT20S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{_pythonw()}</Command><Arguments>-m pmjc --pmjc-home "{home}" service run</Arguments><WorkingDirectory>{cwd}</WorkingDirectory>
  </Exec></Actions>
</Task>"""


def install() -> str:
    if sys.platform == "win32":
        f = config.path("pmjc-task.xml")
        f.write_text(_task_xml(), encoding="utf-16")
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK, "/XML", str(f), "/F"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout)
        subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True)
        return f"scheduled task '{TASK}' created (runs at logon, restarts on failure) and started"
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.pmjc.plist"
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>com.pmjc</string>
<key>ProgramArguments</key><array><string>{sys.executable}</string><string>-m</string><string>pmjc</string><string>service</string><string>run</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>""")
        subprocess.run(["launchctl", "load", "-w", str(plist)])
        return f"launchd agent {plist} loaded"
    unit = Path.home() / ".config" / "systemd" / "user" / "pmjc.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"""[Unit]
Description=pmjc switchboard
After=network-online.target

[Service]
ExecStart={sys.executable} -m pmjc service run
Restart=always
RestartSec=10
Environment=PMJC_HOME={config.home()}

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    subprocess.run(["systemctl", "--user", "enable", "--now", "pmjc.service"])
    return "systemd user unit enabled; run `loginctl enable-linger $USER` so it keeps running after logout"


def uninstall() -> str:
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/End", "/TN", TASK], capture_output=True)
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True, text=True)
        return r.stdout.strip() or r.stderr.strip()
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.pmjc.plist"
        subprocess.run(["launchctl", "unload", "-w", str(plist)])
        plist.unlink(missing_ok=True)
        return "launchd agent removed"
    subprocess.run(["systemctl", "--user", "disable", "--now", "pmjc.service"])
    return "systemd unit disabled"


def status() -> str:
    if sys.platform == "win32":
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"], capture_output=True, text=True)
        return r.stdout.strip() or "not installed"
    if sys.platform == "darwin":
        return subprocess.run(["launchctl", "list", "com.pmjc"], capture_output=True, text=True).stdout or "not loaded"
    return subprocess.run(["systemctl", "--user", "status", "pmjc.service", "--no-pager"], capture_output=True, text=True).stdout


def _single_instance():
    f = open(config.path("supervisor.lock"), "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def _keep_awake() -> None:
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def supervise() -> None:
    """Run the server as a child process and restart it when it exits (with backoff)."""
    lock = _single_instance()
    logf = setup_logging()
    if lock is None:
        log.info("another pmjc supervisor is running; exiting")
        return
    _keep_awake()
    backoff = 5
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    while True:
        started = time.time()
        log.info("starting pmjc server")
        with open(logf.with_name("server.out"), "a", encoding="utf-8") as out:
            p = subprocess.Popen([sys.executable, "-m", "pmjc", "serve"], stdout=out, stderr=subprocess.STDOUT,
                                 creationflags=flags, env=os.environ.copy())
            code = p.wait()
        log.warning("server exited with %s", code)
        backoff = 5 if time.time() - started > 300 else min(backoff * 2, 300)
        time.sleep(backoff)
