"""The claude_cli provider never runs claude through a batch wrapper (cmd.exe mangles prompts and parses & | %)."""
from __future__ import annotations

import shutil

import pytest

from pmjc import agent


def _layout(tmp_path, with_exe=True):
    wrapper = tmp_path / "claude.CMD"
    wrapper.write_text("@echo off\r\n")
    if with_exe:
        real = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"")
        return wrapper, real
    return wrapper, None


def test_batch_wrapper_resolves_to_real_exe(tmp_path, monkeypatch):
    wrapper, real = _layout(tmp_path)
    monkeypatch.setattr(agent.os, "name", "nt")
    monkeypatch.setattr(shutil, "which", lambda _: str(wrapper))
    assert agent._claude_exe() == str(real)


def test_batch_wrapper_without_exe_is_refused(tmp_path, monkeypatch):
    wrapper, _ = _layout(tmp_path, with_exe=False)
    monkeypatch.setattr(agent.os, "name", "nt")
    monkeypatch.setattr(shutil, "which", lambda _: str(wrapper))
    with pytest.raises(RuntimeError, match="batch wrapper"):
        agent._claude_exe()


def test_plain_exe_used_as_is(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(shutil, "which", lambda _: str(exe))
    assert agent._claude_exe() == str(exe)


def test_missing_claude(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="not found"):
        agent._claude_exe()
