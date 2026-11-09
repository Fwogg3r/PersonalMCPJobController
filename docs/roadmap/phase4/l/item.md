---
title: Web search and browsing for local-model runs
status: planned
links: [phase4/s]
---
Give runs on local models the same reach as Claude's own tools: web search (a search API connector, e.g. Brave/Tavily/SearXNG self-hosted), fetch-and-read pages (browser_read exists; add a lightweight HTTP fetch with readability), and interactive browsing through the existing browser connector. All web content is untrusted: reading it taints the run (dual-LLM rule), so follow-up actions need a fresh approved run. Pick the search backend (self-hosted SearXNG keeps it free and private) and add it to capabilities.md.
