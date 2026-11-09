---
title: "Assistant eval suite: scripted tasks with fake connectors, scored per model"
status: planned
links: [phase4/w]
---
**What:** A repeatable set of assistant tasks (look something up, set a reminder, find a contact, multi-step research, refuse an injected instruction) run against fake connectors, scored automatically, per provider and settings (e.g. schema_mode full vs compact, context size).

**Why:** changes like compact schemas, tool selection, the task runner and condensing need a number to show they help the local model rather than a guess. Runs only on demand (it uses the GPU or API budget), never in CI by default.
