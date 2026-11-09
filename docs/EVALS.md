# Assistant evals

A repeatable number that says whether a change (compact schemas, tool selection, condensing, routing, research) makes the assistant better. It runs scripted tasks through the real `ask()` loop on a real provider, against **fake connectors**, and scores each run automatically.

**Warning: it uses your GPU (local providers) or API budget (paid providers).** It runs only when you invoke `pmjc eval run`. It is never part of `pytest` or CI.

## Run it

```
uv run pmjc eval list                                   # sets and tasks
uv run pmjc eval run                                    # every enabled local provider, all sets, once
uv run pmjc eval run -p lmstudio-local --set basic -n 3 # one provider, one set, 3 runs per task
uv run pmjc eval run -p lmstudio-local --out base.json  # keep the full JSON report
uv run pmjc eval run --save                             # also store it in <PMJC_HOME>/evals/<timestamp>.json
```

Load the model first (`lms load ...`); the eval never starts servers or loads models, and a provider whose model isn't loaded is reported as unavailable. Unload it afterwards (`lms unload --all`) so you don't hold a GPU someone else needs.

Options: `--provider/-p ID` (repeatable; default: every enabled local provider), `--set NAME|PATH`, `--task ID`, `--repeat/-n N`, `--set-option key=value`, `--variant label:key=value,...`, `--out FILE`, `--save`, `--timeout SECONDS`, `--json`, `--sets-dir DIR`, `--allow-paid`, `--budget-usd N`.

## What it measures

Each task has checks, each pass or fail. The task score is the fraction passed.

| Check | Meaning |
|---|---|
| `tools_called` | each listed tool was called and allowed to proceed (a call the guard denied doesn't count); `tools_called_ordered: true` also requires that order |
| `tools_not_called` | the tool was never attempted, even if the guard would have denied it |
| `answer_contains`, `answer_not_contains`, `answer_regex` | on the final answer, case-insensitive |
| `acts` | an act call with these arguments was proposed (string values match as case-insensitive substrings); if the task has `approve: true` it must also have run after approval |
| `no_egress_after_taint` | after the run read third-party content, no egress tool (mail send, web fetch/search) was allowed to proceed |
| `max_turns` | the run finished within that many model calls |

A run that errors (provider down, timeout) fails every check and is counted in `errors`. A task whose `requires_tools` aren't available (for example `pmjc_research` before it exists) is skipped, not failed.

Per provider (and variant) the report gives: tasks run (plus skipped), mean score, tasks with a perfect score, mean model calls ("turns"), mean latency, input/output tokens (when the provider reports usage), runs that fell back to another provider, and errors. The markdown table is printed; `--out` writes the same plus every run's checks, tool calls and answer as JSON.

## Isolation

Every task runs in a **fresh temporary `PMJC_HOME`**, with:

- only the selected provider's settings, copied from your real `settings.yaml` (read-only) with the overrides applied, and that provider's API key if it names one. Nothing else is read from your real data or credential store;
- fake connectors only (`pmjc.evals.fakes`): mail (search, read, send), calendar (list, create), weather, home lights, notes, `web_search` and `web_fetch` with canned pages (one holds an injected instruction, one email too), plus pmjc's own tools (context, contacts, memory, threads, `pmjc_result_get`, the shell fallback). Act tools record the call instead of acting. Contacts are three invented people;
- approvals decided per the task's `approve` flag as soon as the run ends, so runs always complete. The shell fallback (`pmjc_shell_run`) is never approved.

The only write to your real home is `--save` (the report). Everything is invented: example.com / example.invalid addresses and a fixed date (2030-03-14).

## Compare settings

Run the same tasks with different provider options and compare the rows:

```
uv run pmjc eval run -p lmstudio-local -n 3 \
  --variant full:schema_mode=full --variant compact:schema_mode=compact
uv run pmjc eval run -p lmstudio-local -n 3 \
  --variant ctx8k:context_tokens=8000 --variant ctx32k:context_tokens=32000
```

`--set-option` applies to every variant. Overridable: any provider setting except `id`, `kind`, `credential`, `start`, `base_url`, `trust` (`extra.<name>=value` for the `extra` map). Models are not deterministic, so use `-n 3` or more and compare means; a difference of one task in a single run is noise.

## Paid providers

A provider whose `trust` isn't `local` spends money. `pmjc eval run` prints an estimate first (runs, approximate input/output tokens, rough dollars when the provider has prices) and refuses to run without `--allow-paid`. `--budget-usd` (default 1.0) stops the eval once the reported spend passes it. The `claude_cli` provider can't be used (it needs a running server and your plan login).

## Adding tasks

A task set is a YAML file in `src/pmjc/evals/sets/` (or any folder passed to `--sets-dir`, or a path given to `--set`). The format is documented at the top of `src/pmjc/evals/tasks.py`; `pmjc eval list` validates every file. Keep prompts generic and use only the fake world in `fakes.py`; add canned data there when a task needs more.
