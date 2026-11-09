---
title: "Assistant eval suite: scripted tasks with fake connectors, scored per model"
status: done
links: [phase4/w]
---
**What:** A repeatable set of assistant tasks (look something up, set a reminder, find a contact, multi-step research, refuse an injected instruction) run against fake connectors, scored automatically, per provider and settings (e.g. schema_mode full vs compact, context size).

**Why:** changes like compact schemas, tool selection, the task runner and condensing need a number to show they help the local model rather than a guess. Runs only on demand (it uses the GPU or API budget), never in CI by default.

**Notes (progress):**
- Done: `pmjc eval list` and `pmjc eval run [--provider ID ...] [--set NAME] [--task ID] [--repeat N] [--variant label:k=v] [--set-option k=v] [--out FILE] [--save] [--allow-paid] [--budget-usd N]`, code in `src/pmjc/evals/`, guide in [docs/EVALS.md](../../../EVALS.md).
- 26 starter tasks in three YAML sets (`basic` 11, `multistep` 8, `safety` 7): lookups, calendar/home/notes acts with approval, mail find and summarise, meeting then message, thread follow-ups, web pages, long-note paging, injection in a page, a search snippet and an email, shell fallback not used, "I can't" answers. The research task needs `pmjc_research` and is skipped until that tool exists.
- Each task runs in a fresh temporary home with only the selected provider's settings (plus its API key) and fake connectors; the real data folder is read only for the provider list, and `--save` is the only write to it. Approvals are decided per task, and the shell fallback is never approved.
- Scoring is pass/fail checks per task (tools called or not, answer text, act arguments, no egress after taint, turn limit); the report gives mean score, perfect tasks, turns, latency, tokens, fallbacks and errors per provider and variant, as markdown and JSON.
- Paid providers need `--allow-paid` after an estimate is shown, with a spend cap. Not yet run against a real model; baseline numbers for compact schemas, tool selection and condensing are still to be collected.
