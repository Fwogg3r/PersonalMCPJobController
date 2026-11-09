"""This PC and your other machines: files (read-only, allowed folders), PC status, SSH hosts, LM Studio hosts."""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from ..connector import Connector, Health, tool

FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
SECRET_GLOBS = ["*secret*", "*token*", "*password*", "*.key", "*.pem", "*.pfx", "*.p12", "id_*", ".env", ".env.*",
                "*.kdbx", "credentials*", "*.enc", "api.key", "*cookie*", "Login Data", "*.db"]


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=FLAGS, stdin=subprocess.DEVNULL)


class Files(Connector):
    id = "files"
    aliases = ['file', 'folder', 'directory', 'document']  # request words that select this connector (tool_mode: select)
    limitations = "Read-only, only inside folders from files_roots; secret-looking files are never readable. Can't write or delete."
    native_alternatives = "your client's file tools if it runs on that machine."
    name = "Files (read-only)"
    description = "List, search and read files inside folders you allow (option `roots`). Secret-looking files are never read."

    def _roots(self) -> list[Path]:
        return [Path(os.path.expandvars(os.path.expanduser(r))).resolve() for r in self.option("roots", [])]

    def configured(self) -> bool:
        return bool(self._roots())

    def _deny(self) -> list[str]:
        return SECRET_GLOBS + list(self.option("deny", []))

    def _check(self, path: str) -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
        if not any(p == r or r in p.parents for r in self._roots()):
            raise PermissionError(f"{path} is outside the allowed folders {[str(r) for r in self._roots()]}")
        for part in p.parts:
            if any(fnmatch.fnmatch(part.lower(), g.lower()) for g in self._deny()):
                raise PermissionError(f"{p.name} looks secret or private; not readable through pmjc")
        return p

    async def health(self) -> Health:
        roots = self._roots()
        if not roots:
            return Health(False, "set option roots (folders pmjc may read)")
        missing = [str(r) for r in roots if not r.exists()]
        return Health(not missing, f"{len(roots)} folders" + (f"; missing {missing}" if missing else ""))

    @tool(scope="read")
    async def files_roots(self) -> list[str]:
        """Folders pmjc may read. Start here: files_list/files_read/files_search paths must be inside one of these."""
        return [str(r) for r in self._roots()]

    @tool(scope="read")
    async def files_list(self, path: str, pattern: str = "*", limit: int = 200) -> list[dict]:
        """Entries in a folder (name, type, size, modified), optionally filtered by a glob pattern. path must be inside a folder from files_roots."""
        p = self._check(path)
        out = []
        for e in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if not fnmatch.fnmatch(e.name, pattern):
                continue
            try:
                self._check(str(e))
            except PermissionError:
                continue
            st = e.stat()
            out.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size, "modified": st.st_mtime})
            if len(out) >= limit:
                break
        return out

    @tool(scope="read", untrusted_output=True)
    async def files_read(self, path: str, max_chars: int = 20000, offset: int = 0) -> dict:
        """Text of a file (UTF-8). path must be inside a folder from files_roots; get paths from files_list or files_search."""
        p = self._check(path)
        if p.is_dir():
            raise IsADirectoryError(str(p))
        if p.stat().st_size > 20_000_000:
            raise ValueError("file is over 20 MB")
        text = p.read_text(encoding="utf-8", errors="replace")
        return {"path": str(p), "size": len(text), "text": text[offset:offset + min(max_chars, 100000)]}

    @tool(scope="read", untrusted_output=True)
    async def files_search(self, query: str, path: str | None = None, glob: str = "*", limit: int = 50) -> list[dict]:
        """Find files under a folder whose name or text contains `query` (case-insensitive). Text search covers files
        under 2 MB."""
        roots = [self._check(path)] if path else self._roots()
        q = query.lower()
        hits: list[dict] = []
        deadline = time.time() + 20

        def walk():
            for root in roots:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".venv", "__pycache__")
                                   and not any(fnmatch.fnmatch(d.lower(), g.lower()) for g in self._deny())]
                    for fn in filenames:
                        if time.time() > deadline or len(hits) >= limit:
                            return
                        if not fnmatch.fnmatch(fn, glob) or any(fnmatch.fnmatch(fn.lower(), g.lower()) for g in self._deny()):
                            continue
                        f = Path(dirpath) / fn
                        if q in fn.lower():
                            hits.append({"path": str(f), "match": "name"})
                            continue
                        try:
                            if f.stat().st_size < 2_000_000:
                                with open(f, encoding="utf-8", errors="ignore") as fh:
                                    for i, line in enumerate(fh, 1):
                                        if q in line.lower():
                                            hits.append({"path": str(f), "line": i, "text": line.strip()[:200]})
                                            break
                        except OSError:
                            pass
        await asyncio.to_thread(walk)
        return hits


class PcStatus(Connector):
    id = "pc"
    aliases = ['cpu', 'ram', 'gpu', 'disk', 'process', 'pc status', 'computer']  # request words that select this connector (tool_mode: select)
    limitations = 'Only the machine pmjc runs on. Read-only.'
    name = "This PC"
    description = "CPU, memory, disks, GPU, uptime and the busiest processes on the PC running pmjc."

    async def health(self) -> Health:
        return Health(True, "ready")

    @tool(scope="read")
    async def pc_status(self) -> dict:
        """CPU and memory load, free disk space, GPU use (NVIDIA) and uptime."""
        import psutil
        disks = {}
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
                disks[part.mountpoint] = {"free_gb": round(u.free / 1e9, 1), "used_pct": u.percent}
            except OSError:
                pass
        vm = psutil.virtual_memory()
        out = {"host": os.environ.get("COMPUTERNAME") or os.uname().nodename,
               "cpu_pct": psutil.cpu_percent(interval=0.5), "memory_used_pct": vm.percent,
               "memory_free_gb": round(vm.available / 1e9, 1), "disks": disks,
               "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1)}
        smi = shutil.which("nvidia-smi")
        if smi:
            r = await asyncio.to_thread(_run, [smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                                               "--format=csv,noheader,nounits"])
            out["gpus"] = [dict(zip(["name", "util_pct", "mem_used_mb", "mem_total_mb", "temp_c"], [x.strip() for x in l.split(",")]))
                           for l in r.stdout.strip().splitlines() if l.strip()]
        return out

    @tool(scope="read")
    async def pc_processes(self, sort_by: str = "memory", limit: int = 15) -> list[dict]:
        """Busiest processes by memory or cpu."""
        import psutil
        procs = []
        for p in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
            try:
                procs.append({"name": p.info["name"], "mem_mb": round(p.info["memory_info"].rss / 1e6),
                              "cpu_pct": p.info["cpu_percent"]})
            except Exception:
                pass
        key = "cpu_pct" if sort_by == "cpu" else "mem_mb"
        return sorted(procs, key=lambda x: x[key] or 0, reverse=True)[:min(limit, 100)]


def ssh_aliases() -> list[str]:
    cfg = Path.home() / ".ssh" / "config"
    if not cfg.exists():
        return []
    return [h for line in cfg.read_text(errors="ignore").splitlines()
            if line.strip().lower().startswith("host ") for h in line.split()[1:] if "*" not in h and "?" not in h]


HOST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9\[][A-Za-z0-9._@:%+\[\]-]{0,254}$")  # hosts, IPv6, users; never a leading "-"


class Ssh(Connector):
    """Any machine you can reach with OpenSSH and a key.

    Machines come from option `machines` (settings.yaml, or `pmjc ssh add`):
        machines:
          nas:      {host: 192.0.2.10, user: admin, port: 22, key: ~/.ssh/id_ed25519}
          gpu-box:  {host: gpu.example.com, user: me, jump: bastion.example.com}
          office:   {host: office-pc, proxy_command: "cloudflared access ssh --hostname %h"}
          builder:  {alias: builder}          # an existing Host alias from ~/.ssh/config
    If `machines` isn't set, every Host alias in ~/.ssh/config is available (set `use_ssh_config: false` to stop that).
    Login is always non-interactive (BatchMode): keys or an agent, never passwords.
    """
    id = "ssh"
    aliases = ['ssh', 'server', 'remote machine', 'run command', 'script']  # request words that select this connector (tool_mode: select)
    limitations = 'Only machines configured in pmjc (ssh_machines); every command needs approval. No interactive sessions.'
    native_alternatives = "your client's shell if it runs on that machine."
    name = "Remote machines (SSH)"
    description = ("Check and run commands on other machines over SSH: any host you configure (address, user, port, key, "
                   "jump host or proxy command) or Host aliases from ~/.ssh/config. Running commands needs approval.")
    docs_url = "https://man.openbsd.org/ssh_config"

    def _machines(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if self.option("use_ssh_config", True) and self.option("machines") is None:
            out = {a: {"alias": a} for a in ssh_aliases()}
        for name, m in (self.option("machines") or {}).items():
            out[name] = m if isinstance(m, dict) else {"alias": str(m)}
        return out

    def configured(self) -> bool:
        return bool(shutil.which("ssh")) and bool(self._machines())

    def _argv(self, name: str, command: str) -> list[str]:
        machines = self._machines()
        if name not in machines:
            raise PermissionError(f"{name} isn't a configured machine: {sorted(machines)}")
        m = machines[name]
        argv = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
        if m.get("alias"):
            if not HOST_NAME.match(str(m["alias"])):
                raise ValueError("bad alias")
            return argv + [str(m["alias"]), command]
        host = str(m.get("host", ""))
        if not SAFE_VALUE.match(host):
            raise ValueError(f"machine {name}: 'host' is missing or invalid")
        if m.get("port"):
            argv += ["-p", str(int(m["port"]))]
        if m.get("key"):
            argv += ["-i", os.path.expanduser(os.path.expandvars(str(m["key"])))]
        if m.get("jump"):
            if not SAFE_VALUE.match(str(m["jump"])):
                raise ValueError(f"machine {name}: invalid 'jump'")
            argv += ["-J", str(m["jump"])]
        if m.get("proxy_command"):
            argv += ["-o", f"ProxyCommand={m['proxy_command']}"]  # from the owner's settings file only
        if m.get("strict_host_key_checking") is False:
            argv += ["-o", "StrictHostKeyChecking=accept-new"]
        user = m.get("user")
        if user and not SAFE_VALUE.match(str(user)):
            raise ValueError(f"machine {name}: invalid 'user'")
        return argv + [f"{user}@{host}" if user else host, command]

    @tool(scope="read")
    async def ssh_machines(self) -> list[dict]:
        """Machines pmjc can reach, with how each is addressed (no keys or secrets)."""
        return [{"name": n, **({"alias": m["alias"]} if m.get("alias") else
                               {"host": m.get("host"), "user": m.get("user"), "port": m.get("port", 22)})}
                for n, m in self._machines().items()]

    async def _ssh(self, name: str, command: str, timeout: int) -> dict:
        r = await asyncio.to_thread(_run, self._argv(name, command), timeout)
        return {"exit_code": r.returncode, "stdout": r.stdout[-20000:], "stderr": r.stderr[-4000:]}

    @tool(scope="read")
    async def ssh_check(self, machine: str) -> dict:
        """Is the machine reachable? Returns its hostname and uptime (runs no other command). machine is a name from ssh_machines."""
        cmd = ("hostname && (uptime 2>/dev/null || powershell -NoProfile -Command "
               "\"[math]::Round(((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalHours, 1)\")")
        return await self._ssh(machine, cmd, 40)

    @tool(scope="act")
    async def ssh_run(self, machine: str, command: str, timeout_seconds: int = 120) -> dict:
        """Run a shell command on a machine over SSH. Needs approval. machine is a name from ssh_machines."""
        return await self._ssh(machine, command, min(max(timeout_seconds, 5), 1800))


class LmStudio(Connector):
    id = "lmstudio"
    aliases = ['lm studio', 'local model', 'llm']  # request words that select this connector (tool_mode: select)
    limitations = 'Local models only; ask uses already-loaded models, loading needs approval and uses GPU memory. Unload when done.'
    name = "LM Studio hosts"
    description = "Your local models: what's downloaded and loaded on each host, and asking one directly."

    def _hosts(self) -> dict[str, str]:
        return self.option("hosts", {"local": "http://127.0.0.1:1234"})

    async def health(self) -> Health:
        up = [n for n, u in self._hosts().items() if await self._models(u) is not None]
        return Health(bool(up), f"up: {', '.join(up) or 'none'}")

    async def _models(self, url: str) -> list[dict] | None:
        try:
            async with httpx.AsyncClient(timeout=4) as c:
                r = await c.get(url.rstrip("/") + "/api/v0/models")
                return r.json().get("data", []) if r.status_code == 200 else None
        except Exception:
            return None

    def _url(self, host: str | None) -> str:
        hosts = self._hosts()
        if host is None:
            return next(iter(hosts.values()))
        if host not in hosts:
            raise ValueError(f"unknown host; choose from {list(hosts)}")
        return hosts[host]

    @tool(scope="read")
    async def lmstudio_status(self) -> dict:
        """Each host: reachable or not, and its models with loaded / not-loaded state."""
        out = {}
        for name, url in self._hosts().items():
            ms = await self._models(url)
            out[name] = "unreachable" if ms is None else [{"id": m["id"], "state": m.get("state"), "type": m.get("type")} for m in ms]
        return out

    @tool(scope="read")
    async def lmstudio_ask(self, prompt: str, model: str | None = None, host: str | None = None, max_tokens: int = 2000) -> dict:
        """Ask a model that is ALREADY LOADED on a host (this never loads one). Stays on your machines."""
        url = self._url(host)
        ms = await self._models(url) or []
        loaded = [m["id"] for m in ms if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm")]
        if not loaded:
            return {"error": "no model is loaded on that host; load one first (lmstudio_load) or ask the user"}
        use = model if model in loaded else loaded[0]
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(url.rstrip("/") + "/v1/chat/completions",
                             json={"model": use, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens})
            r.raise_for_status()
            m = r.json()["choices"][0]["message"]
        return {"model": use, "answer": m.get("content") or "", "reasoning": (m.get("reasoning_content") or "")[:2000]}

    @tool(scope="act")
    async def lmstudio_load(self, model: str, host: str | None = None) -> dict:
        """Load a model into GPU/CPU memory on a host (uses VRAM until unloaded). Needs approval."""
        url = self._url(host)
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(url.rstrip("/") + "/v1/chat/completions",
                             json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
            return {"status": r.status_code, "loaded": model if r.status_code == 200 else None}

    @tool(scope="act")
    async def lmstudio_unload(self) -> dict:
        """Unload every model from the LM Studio on THIS machine (frees GPU memory). For other machines use ssh_run
        with `lms unload --all`. Needs approval."""
        lms = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms"))
        if not Path(lms).exists():
            raise FileNotFoundError("LM Studio's lms command isn't installed on this machine")
        r = await asyncio.to_thread(_run, [lms, "unload", "--all"], 120)
        return {"exit_code": r.returncode, "output": (r.stdout + r.stderr)[-1000:]}
