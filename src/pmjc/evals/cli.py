"""`pmjc eval ...`: list task sets, run them against configured providers. Registered in pmjc.cli."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer

eval_app = typer.Typer(help="Assistant eval suite: scripted tasks on fake connectors, scored per provider (see docs/EVALS.md). "
                            "Uses the GPU or API budget, so it only runs when you invoke it.")


def _fail(msg: str, code: int = 2):
    typer.echo(msg, err=True)
    raise typer.Exit(code)


@eval_app.command("list")
def list_cmd(sets_dir: Optional[Path] = typer.Option(None, "--sets-dir", help="Extra folder of task-set YAML files.")):
    """Show the task sets and their tasks."""
    from . import tasks
    try:
        sets = tasks.load_sets(extra_dir=sets_dir)
    except tasks.TaskError as e:
        _fail(str(e))
    total = 0
    for s in sets:
        typer.echo(f"{s.set}: {s.description} ({len(s.tasks)} tasks)")
        for t in s.tasks:
            extra = f"  [needs {', '.join(t.requires_tools)}]" if t.requires_tools else ""
            typer.echo(f"  {t.id:<28} {t.category:<14} {t.prompt[:70]}{extra}")
        total += len(s.tasks)
    typer.echo(f"{total} tasks in {len(sets)} sets")


@eval_app.command("run")
def run_cmd(
    provider: list[str] = typer.Option([], "--provider", "-p", help="Provider id from settings (repeatable). Default: every enabled local provider."),
    set_: list[str] = typer.Option([], "--set", help="Task set name or YAML path (repeatable). Default: all bundled sets."),
    task: list[str] = typer.Option([], "--task", help="Only these task ids (repeatable)."),
    repeat: int = typer.Option(1, "--repeat", "-n", min=1, help="Runs per task (models are not deterministic)."),
    set_option: list[str] = typer.Option([], "--set-option", help="Provider option override, e.g. schema_mode=compact (repeatable)."),
    variant: list[str] = typer.Option([], "--variant", help="Compare settings: label:key=value,key=value (repeatable)."),
    out: Optional[Path] = typer.Option(None, "--out", help="Write the full JSON report here."),
    save: bool = typer.Option(False, "--save", help="Also save the JSON under <PMJC_HOME>/evals/ (the only write to the real data folder)."),
    timeout: float = typer.Option(600, "--timeout", help="Seconds per task."),
    allow_paid: bool = typer.Option(False, "--allow-paid", help="Allow providers that are not local (spends API budget)."),
    budget_usd: float = typer.Option(1.0, "--budget-usd", help="Stop a run once this much was spent (non-local providers)."),
    sets_dir: Optional[Path] = typer.Option(None, "--sets-dir", help="Extra folder of task-set YAML files."),
    json_only: bool = typer.Option(False, "--json", help="Print the JSON report instead of the markdown table."),
):
    """Run the eval on real providers in an isolated temporary data folder with fake connectors."""
    import json
    from .. import config
    from . import runner, tasks
    real_home = config.home()
    try:
        sets = tasks.load_sets(set_ or None, extra_dir=sets_dir)
        all_t = tasks.all_tasks(sets)
        if task:
            unknown = set(task) - {t.id for t in all_t}
            if unknown:
                _fail(f"unknown task id(s): {', '.join(sorted(unknown))}")
            all_t = [t for t in all_t if t.id in task]
        base = runner.parse_overrides(set_option)
        variants = [runner.Variant(v.label, {**base, **v.overrides}) for v in map(runner.parse_variant, variant)] \
            or [runner.Variant("default", base)]
        have = {p.id: p for p in runner.real_providers()}
        if provider:
            missing = [i for i in provider if i not in have]
            if missing:
                _fail(f"no such provider: {', '.join(missing)} (have: {', '.join(have)})")
            chosen = [have[i] for i in provider]
        else:
            chosen = [p for p in have.values() if p.enabled and not runner.is_paid(p) and p.kind not in runner.UNSUPPORTED_KINDS]
            if not chosen:
                _fail("no enabled local provider in settings; pass --provider ID")
        for p in chosen:  # validate the overrides before anything runs
            for v in variants:
                runner.apply_overrides(p, v.overrides)
        paid = [p for p in chosen if runner.is_paid(p)]
        for p in paid:
            e = runner.estimate(all_t, runner.apply_overrides(p, variants[0].overrides), repeat, len(variants))
            typer.echo(f"{p.id}: about {e['runs']} runs, ~{e['tokens_in']:,} input + ~{e['tokens_out']:,} output tokens"
                       + (f", roughly ${e['usd']}" if e["priced"] else " (no price set for this provider)"), err=True)
        if paid and not allow_paid:
            _fail("these providers are not local and spend API budget: " + ", ".join(p.id for p in paid)
                  + ". Re-run with --allow-paid to proceed (see the estimate above; --budget-usd caps it).")
        if any(not runner.is_paid(p) for p in chosen):
            typer.echo("note: this uses your local GPU; unload the model afterwards (lms unload --all).", err=True)
        secrets = runner.read_secrets(chosen)
        report = asyncio.run(runner.run_eval(all_t, chosen, variants, repeat, timeout, secrets,
                                             budget_usd if paid else None, progress=lambda s: typer.echo(s, err=True),
                                             checkpoint=out))
    except (tasks.TaskError, runner.EvalError) as e:
        _fail(str(e))
    typer.echo(json.dumps(report, indent=2, default=str) if json_only else runner.render_markdown(report))
    if out:
        typer.echo(f"wrote {runner.save_report(report, out)}", err=True)
    if save:
        typer.echo(f"saved {runner.save_report(report, runner.default_save_path(real_home))}", err=True)


@eval_app.command("smoke")
def smoke_cmd(
    provider: str = typer.Option(..., "--provider", "-p", help="A LOCAL provider id (your real data goes to the model)."),
    connector: list[str] = typer.Option([], "--connector", "-c", help="Only these smoke sets (connector names, repeatable)."),
    task: list[str] = typer.Option([], "--task", help="Only these task ids (repeatable)."),
    repeat: int = typer.Option(1, "--repeat", "-n", min=1),
    variant: list[str] = typer.Option([], "--variant", help="Compare settings: label:key=value,... (e.g. adaptive:thinking.policy=adaptive)."),
    set_option: list[str] = typer.Option([], "--set-option", help="Provider option override for every variant."),
    out: Optional[Path] = typer.Option(None, "--out", help="Write the full JSON report here."),
    timeout: float = typer.Option(300, "--timeout", help="Seconds per task."),
    show_coverage: bool = typer.Option(False, "--coverage", help="Only list real tools that have no smoke prompt."),
):
    """Drive every real tool with a local model: reads run for real, acts are only recorded (see docs/EVALS.md)."""
    import json
    from . import runner, smoke
    have = {p.id: p for p in runner.real_providers()}
    if provider not in have:
        _fail(f"no such provider: {provider} (have: {', '.join(have)})")
    p = have[provider]
    if runner.is_paid(p):
        _fail("the smoke test sends your real data (files, messages, playback) to the model: local providers only")
    try:
        if show_coverage:
            missing = smoke.coverage(p)
            for cid, names in sorted(missing.items()):
                typer.echo(f"{cid}: {', '.join(sorted(names))}")
            typer.echo(f"{sum(len(v) for v in missing.values())} tools without a smoke prompt")
            return
        all_t = smoke.load_tasks(connector or None)
        if task:
            all_t = [t for t in all_t if t.id in task]
        if not all_t:
            _fail("no smoke tasks match")
        base = runner.parse_overrides(set_option)
        variants = [runner.Variant(v.label, {**base, **v.overrides}) for v in map(runner.parse_variant, variant)]             or [runner.Variant("default", base)]
        for v in variants:
            runner.apply_overrides(p, v.overrides)
        typer.echo("note: reads run against your real services; acts are recorded, never run.", err=True)
        report = asyncio.run(runner.run_eval(all_t, [p], variants, repeat, timeout, None, None,
                                             progress=lambda s: typer.echo(s, err=True), home=smoke.live_home,
                                             checkpoint=out))
    except (tasks_error() + (runner.EvalError,)) as e:
        _fail(str(e))
    typer.echo(runner.render_markdown(report))
    if out:
        typer.echo(f"wrote {runner.save_report(report, out)}", err=True)


def tasks_error() -> tuple:
    from . import tasks
    return (tasks.TaskError,)
