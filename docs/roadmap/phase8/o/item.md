---
title: "claude_cli provider: run claude.exe, never the npm claude.cmd wrapper"
status: done
links: [phase4/aa]
---
Found 2026-11-10 while testing Spotify from Discord: the second message in a thread failed with claude-plan JSONDecodeError. On Windows shutil.which('claude') returns npm's claude.CMD, so the prompt went through cmd.exe, which cuts arguments at the first newline (thread history) and exits 0, so claude ran without --output-format json and the rest of the flags. cmd.exe also parses & | % in arguments, a command-injection path for third-party text. Fix: agent._claude_exe() uses node_modules/@anthropic-ai/claude-code/bin/claude.exe next to the wrapper and refuses any batch file; empty stdout now gives a clear error. Tests: tests/test_claude_exe.py.
