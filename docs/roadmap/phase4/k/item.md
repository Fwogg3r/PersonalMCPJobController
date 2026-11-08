---
title: "Show the model's work in Discord: thinking, tool calls, streamed answers"
status: planned
links: []
---
Everything the model produces should reach the owner: a 'thinking...' placeholder that is edited as the run progresses (status: calling tool X, waiting for approval, falling back to Y), streamed answer text (edit the message every ~1 s within Discord's rate limits, then split at 2000 chars), and reasoning/think output where the provider returns it (LM Studio reasoning_content, Claude thinking blocks), shown in a collapsed/subtext form or the #logs channel. Needs streaming in agent._openai and _anthropic, a progress callback on Caller (like on_tool), and edit-in-place helpers in discord_door. Tool trace (/pmjc trace) and the log channel already exist and become part of this.
