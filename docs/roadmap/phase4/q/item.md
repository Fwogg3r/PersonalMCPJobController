---
title: Local model at 64k context (measured ceiling on a 24 GB GPU)
status: done
links: [phase4/p]
---
Raised the local model from 32k to 64k context. Measured on an RTX 3090: 32k 21.7 GB / 59.9 tok/s; 64k 22.7 GB / 56.7 tok/s; 96k fills the card and spills 1.4 GB to system RAM, 17.8 tok/s; 128k spills 3.7 GB, 13.1 tok/s. Keep 1-1.5 GB free for other apps. Options for more context and the full reasoning: docs/DECISIONS.md, "Local model context size". The setting itself lives in the user's settings (provider context_tokens), not in the repo.
