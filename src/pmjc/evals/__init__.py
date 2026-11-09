"""Assistant eval suite: scripted tasks against fake connectors, scored per provider and setting.

It runs only when asked (`pmjc eval run`), never in pytest or CI by default, because it uses the GPU or API budget.
See docs/EVALS.md.
"""
