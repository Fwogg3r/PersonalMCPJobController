"""A local model already loaded with a smaller context than the provider's context_tokens (e.g. LM Studio's JIT load
at 8192 tokens, started by another client) is reloaded with the right context before a run, and a run that hits
LM Studio's exceed_context_size_error reloads once and retries. LM Studio is faked: its REST API through an httpx
MockTransport, the `lms` CLI through router._run / router._lms_ps."""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from test_context import home  # noqa: F401  (autouse: temp PMJC_HOME)

_REAL_CLIENT = httpx.AsyncClient
CTX_ERROR = {"error": {"type": "exceed_context_size_error", "message": "The number of tokens to keep from the initial "
                       "prompt is greater than the context length (n_keep: 16231 >= n_ctx: 8192)"}}


class FakeLMStudio:
    """The model `m` loaded at `ctx` tokens (None: not loaded); `lms unload` / `lms load --context-length N` change it."""

    def __init__(self, monkeypatch, ctx=8192, report_ctx=True, status="idle"):
        from pmjc import router
        self.ctx, self.report_ctx, self.status = ctx, report_ctx, status
        self.commands: list[list[str]] = []
        self.chat: list = []  # scripted /chat/completions responses (httpx.Response), popped in order
        self.ps_calls = 0

        def run(cmd, timeout):
            argv = [str(a) for a in cmd]
            self.commands.append(argv)
            if argv[1] == "unload":
                self.ctx = None
            elif argv[1] == "load":
                self.ctx = int(argv[argv.index("--context-length") + 1]) if "--context-length" in argv else 4096
            return True

        def ps(p):
            self.ps_calls += 1
            if self.ps_calls > 2:
                self.status = "idle"  # the other client finishes after a couple of checks
            return [] if self.ctx is None else [{"identifier": "m", "modelKey": "m", "contextLength": self.ctx,
                                                 "status": self.status}]

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/v0/models":
                row = {"id": "m", "state": "not-loaded" if self.ctx is None else "loaded", "max_context_length": 262144}
                if self.ctx is not None and self.report_ctx:
                    row["loaded_context_length"] = self.ctx
                return httpx.Response(200, json={"data": [row]})
            if req.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "m"}]})
            if req.url.path == "/v1/chat/completions":
                return self.chat.pop(0)
            return httpx.Response(404)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
        monkeypatch.setattr(router, "_run", run)
        monkeypatch.setattr(router, "_lms_ps", ps)
        monkeypatch.setattr(router, "_lms_exe", lambda p: "lms")
        monkeypatch.setattr(router, "_lms_load", lambda p: ["lms", "load", p.model, "--context-length",
                                                            str(p.context_tokens), "-y"])
        monkeypatch.setattr(router, "RELOAD_BUSY_WAIT_S", 0.05)

        async def no_sleep(s):
            return None
        monkeypatch.setattr(router.asyncio, "sleep", no_sleep)
        router._reload_locks.clear()


def _prov(**kw):
    from pmjc import config
    kw.setdefault("stream", False)
    return config.ProviderConfig(id="loc", kind="openai_compat", base_url="http://127.0.0.1:1234/v1", trust="local",
                                 model="m", context_tokens=65536, **kw)


def _auto_start(on=True):
    from pmjc import config
    s = config.load()
    s.auto_start_local = on
    config.save(s)


async def test_healthy_reloads_a_model_loaded_with_a_small_context(monkeypatch):
    from pmjc import router
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=8192)
    assert await router.healthy(_prov()) is True
    assert [c[1] for c in lm.commands] == ["unload", "load"]
    assert lm.commands[1][lm.commands[1].index("--context-length") + 1] == "65536"
    assert lm.ctx == 65536


async def test_no_reload_when_the_context_is_big_enough(monkeypatch):
    from pmjc import router
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=65536)
    assert await router.healthy(_prov()) is True
    assert await router.ensure_context(_prov()) == "ok"
    assert lm.commands == []


async def test_context_from_lms_ps_when_the_api_does_not_report_it(monkeypatch):
    from pmjc import router
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=8192, report_ctx=False)
    assert await router.loaded_context(_prov()) == 8192
    assert await router.ensure_context(_prov()) == "reloaded"
    assert lm.ctx == 65536


async def test_gpu_guard_blocks_the_reload(monkeypatch):
    from pmjc import router
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=8192)
    monkeypatch.setattr(router, "gpu_busy", lambda thr, processes=None: True)
    assert await router.ensure_context(_prov(extra={"skip_when_gpu_busy_percent": 50})) == "short"
    assert lm.commands == []


async def test_no_reload_while_pmjc_has_a_request_in_flight(monkeypatch):
    from pmjc import router
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=8192)
    with router.local_inflight():
        assert await router.ensure_context(_prov()) == "short"
    assert lm.commands == []


async def test_no_reload_when_auto_start_or_load_model_is_off(monkeypatch):
    from pmjc import router
    lm = FakeLMStudio(monkeypatch, ctx=8192)
    _auto_start(False)
    assert await router.ensure_context(_prov()) == "short"
    _auto_start(True)
    assert await router.ensure_context(_prov(load_model=False)) == "short"
    assert lm.commands == []


async def test_waits_for_another_clients_generation_then_reloads(monkeypatch, caplog):
    from pmjc import router
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=8192, status="generating")
    assert await router.ensure_context(_prov()) == "reloaded"
    assert lm.ps_calls >= 2 and [c[1] for c in lm.commands] == ["unload", "load"]


async def test_unloading_a_busy_model_is_logged(monkeypatch, caplog):
    from pmjc import router
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=8192, status="generating")
    lm.ps_calls = -10**6  # never finishes
    with caplog.at_level(logging.WARNING, logger="pmjc.router"):
        assert await router.ensure_context(_prov()) == "reloaded"
    assert any("still using it" in r.getMessage() for r in caplog.records)


async def test_not_ready_without_start_when_a_reload_is_possible(monkeypatch):
    """Interactive requests check with start=False: a short context reads as not ready so the background start
    reloads it; with auto-start off the model is used as is."""
    from pmjc import router
    lm = FakeLMStudio(monkeypatch, ctx=8192)
    _auto_start(True)
    assert await router.healthy(_prov(), start=False) is False
    _auto_start(False)
    assert await router.healthy(_prov(), start=False) is True
    assert lm.commands == []


def test_context_error_matches_lm_studio_messages():
    from pmjc import router
    assert router.context_error(f"HTTP 400 from loc: {json.dumps(CTX_ERROR)}")
    assert router.context_error("loc: request exceeds the available context size")
    assert not router.context_error("HTTP 500 from loc: model crashed")


def _caller():
    from pmjc import auth
    return auth.Caller("t", "remote", {"sources", "jobs"})


async def test_run_reloads_and_retries_once_on_exceed_context_size_error(monkeypatch):
    from pmjc import agent
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=8192)
    lm.chat = [httpx.Response(400, json=CTX_ERROR),
               httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {}})]
    res = await agent._openai(_prov(), "hello", _caller(), [], "sys", 2)
    assert res.text == "hi"
    assert [c[1] for c in lm.commands] == ["unload", "load"] and lm.ctx == 65536


async def test_streamed_run_retries_too(monkeypatch):
    from pmjc import agent
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=8192)
    sse = (f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': 'hi'}, 'finish_reason': None}]})}\n\n"
           "data: [DONE]\n\n").encode()
    lm.chat = [httpx.Response(400, json=CTX_ERROR),
               httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})]
    res = await agent._openai(_prov(stream=True), "hello", _caller(), [], "sys", 2)
    assert res.text == "hi" and lm.ctx == 65536


async def test_no_retry_when_the_context_is_already_full(monkeypatch):
    """The prompt really is too big for context_tokens: no reload, the error goes up (the router tries the next one)."""
    from pmjc import agent
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=65536)
    lm.chat = [httpx.Response(400, json=CTX_ERROR)]
    with pytest.raises(RuntimeError, match="exceed_context_size"):
        await agent._openai(_prov(), "hello", _caller(), [], "sys", 2)
    assert lm.commands == []


async def test_no_retry_for_other_errors(monkeypatch):
    from pmjc import agent
    _auto_start()
    lm = FakeLMStudio(monkeypatch, ctx=8192)
    lm.chat = [httpx.Response(500, json={"error": "boom"})]
    with pytest.raises(RuntimeError, match="boom"):
        await agent._openai(_prov(), "hello", _caller(), [], "sys", 2)
    assert lm.commands == []
