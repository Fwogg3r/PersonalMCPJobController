"""Eval task format and loader. One YAML file per task set (src/pmjc/evals/sets/*.yaml, or any path):

    set: basic
    description: ...
    tasks:
      - id: weather_lookup
        category: lookup
        prompt: What's the weather in Springfield?
        thread: [{user: "...", assistant: "..."}]   # optional earlier turns
        approve: true                                # how approvals are decided; the run never waits for a person
        max_turns: 6
        requires_tools: [pmjc_research]              # skipped when a tool does not exist
        expect:
          tools_called: [weather_forecast]
          tools_called_ordered: false                # true: tools_called must also appear in this order
          tools_not_called: [pmjc_shell_run]
          answer_contains: [rain]                    # case-insensitive substrings
          answer_not_contains: []
          answer_regex: ['\\b9\\b']                  # case-insensitive
          acts: [{tool: mail_send, args: {to: dana.lopez@example.com}}]   # string values match as substrings
          no_egress_after_taint: true
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

SETS_DIR = Path(__file__).parent / "sets"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Turn(_Strict):
    user: str
    assistant: str


class Act(_Strict):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class Expect(_Strict):
    tools_called: list[str] = Field(default_factory=list)
    tools_called_ordered: bool = False
    tools_not_called: list[str] = Field(default_factory=list)
    answer_contains: list[str] = Field(default_factory=list)
    answer_not_contains: list[str] = Field(default_factory=list)
    answer_regex: list[str] = Field(default_factory=list)
    acts: list[Act] = Field(default_factory=list)
    no_egress_after_taint: bool = False

    @field_validator("answer_regex")
    @classmethod
    def _regex_compiles(cls, v: list[str]) -> list[str]:
        for r in v:
            re.compile(r)
        return v


class Task(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    prompt: str = Field(min_length=1)
    category: str = ""
    thread: list[Turn] = Field(default_factory=list)
    expect: Expect = Field(default_factory=Expect)
    max_turns: int = Field(default=8, ge=1, le=30)
    approve: bool = True
    requires_tools: list[str] = Field(default_factory=list)
    set: str = ""  # filled by the loader


class TaskSet(_Strict):
    set: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = ""
    tasks: list[Task]


class TaskError(ValueError):
    pass


def load_file(path: Path) -> TaskSet:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ts = TaskSet.model_validate(data)
    except Exception as e:
        raise TaskError(f"{path.name}: {e}") from e
    seen: set[str] = set()
    for t in ts.tasks:
        if t.id in seen:
            raise TaskError(f"{path.name}: duplicate task id {t.id}")
        seen.add(t.id)
        t.set = ts.set
        e = t.expect
        if not (e.tools_called or e.tools_not_called or e.answer_contains or e.answer_not_contains or e.answer_regex
                or e.acts or e.no_egress_after_taint):
            raise TaskError(f"{path.name}: task {t.id} has no expectations")
    return ts


def available_sets(extra_dir: Path | None = None) -> dict[str, Path]:
    out = {p.stem: p for p in sorted(SETS_DIR.glob("*.y*ml"))}
    if extra_dir:
        out.update({p.stem: p for p in sorted(Path(extra_dir).glob("*.y*ml"))})
    return out


def load_sets(names: list[str] | None = None, extra_dir: Path | None = None) -> list[TaskSet]:
    """Named sets (a name from `available_sets`, or a path to a YAML file); all bundled sets when no name is given."""
    avail = available_sets(extra_dir)
    out: list[TaskSet] = []
    for n in names or list(avail):
        p = avail.get(n) or (Path(n) if Path(n).is_file() else None)
        if p is None:
            raise TaskError(f"no task set {n!r}; available: {', '.join(avail) or 'none'}")
        out.append(load_file(p))
    ids = [t.id for s in out for t in s.tasks]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise TaskError(f"duplicate task ids across sets: {', '.join(sorted(dup))}")
    return out


def all_tasks(sets: list[TaskSet]) -> list[Task]:
    return [t for s in sets for t in s.tasks]
