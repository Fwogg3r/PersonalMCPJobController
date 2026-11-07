"""Ask: one plain-language request in, one answer out. Used by REST (/api/ask), the CLI, the dashboard, phones.

The routed model gets the read tools the caller may use plus the watch tools, and follows the request flow:
answer from connectors right away, or start a watch when waiting is needed. Results of watches arrive later on
the user's notification channel and through /api/watches/{id}.
"""
from __future__ import annotations

from . import agent, db, registry, router
from .auth import Caller

ASK_SYSTEM = agent.SYSTEM + """

How to handle the user's request:
1. Call pmjc_connectors if you're unsure what's connected.
2. If the connected tools can answer now, call them and answer. Don't start a watch.
3. If the answer needs waiting (a later time, a reply that hasn't arrived, a change you must detect), call
   pmjc_watch_start once with the right type (time, event or poll) and the narrowest match. Then tell the user, in
   one or two lines, what you're watching for, how long, and that the answer will be sent to them when it fires.
4. Never start more than one watch per request, and never poll faster than needed.
5. If the user asks for something to happen regularly ("every morning", "each hour"), create a job with
   pmjc_job_create instead of a watch."""

WATCH_TOOLS = {"pmjc_connectors", "pmjc_watch_start", "pmjc_watch_status", "pmjc_watch_cancel", "pmjc_watch_list",
               "pmjc_job_create", "pmjc_jobs_list"}
DDL = "CREATE TABLE IF NOT EXISTS asks (id INTEGER PRIMARY KEY, text TEXT, norm TEXT, ts REAL, answer TEXT, provider TEXT, caller TEXT)"


def normalize(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


def record(text: str, answer: str, provider: str, caller: str) -> None:
    import time
    db.x(DDL)
    db.x("INSERT INTO asks(text,norm,ts,answer,provider,caller) VALUES(?,?,?,?,?,?)",
         text[:4000], normalize(text)[:500], time.time(), answer[:4000], provider, caller)


def repeated(min_count: int = 2, limit: int = 20) -> list[dict]:
    """Requests asked more than once: candidates for automation."""
    db.x(DDL)
    return db.q("SELECT norm, MAX(text) AS text, COUNT(*) AS times, MAX(ts) AS last FROM asks GROUP BY norm "
                "HAVING COUNT(*) >= ? ORDER BY times DESC, last DESC LIMIT ?", min_count, limit)


def recent(limit: int = 20) -> list[dict]:
    db.x(DDL)
    return db.q("SELECT id, text, ts, provider FROM asks ORDER BY ts DESC LIMIT ?", limit)


async def ask(caller: Caller, text: str, private: bool = False, provider_order: list[str] | None = None) -> dict:
    tools = [t for t in registry.get().tools()
             if (t.scope == "read" and caller.can("sources")) or (t.name in WATCH_TOOLS and caller.can("jobs"))
             or (t.scope == "act" and caller.can("actions"))]
    started_before = {r["id"] for r in db.q("SELECT id FROM watches WHERE created_by=?", caller.name)}
    res = await agent.run(text, caller, tools, router.Need(private=private, order=provider_order), system=ASK_SYSTEM,
                          max_turns=10)
    watches = [r["id"] for r in db.q("SELECT id FROM watches WHERE created_by=?", caller.name) if r["id"] not in started_before]
    record(text, res.text, res.provider, caller.name)
    return {"answer": res.text, "provider": res.provider, "watches": watches, "cost_usd": round(res.cost_usd, 5)}
