---
title: Service stop also stops the server (Windows job object)
status: done
links: []
---
Found while restarting: `schtasks /End` killed only the supervisor, so the server it started kept running and held the port, and every new server exited with code 3 (address in use) until the old one was killed by hand. The supervisor now puts itself in a Windows job object with KILL_ON_JOB_CLOSE; children inherit it, so when the supervisor exits or is killed, the server tree goes with it. systemd/launchd already stop the process group. Test: tests/test_service_job.py (kill the parent, the child dies). Checked on the real service: stop left no server; restart came up cleanly.
