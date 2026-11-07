# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Generate docs/CAPABILITIES.md from pmjc.capabilities.CATALOG. Run: uv run scripts/gen_capabilities.py"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "src"))
import importlib.util

spec = importlib.util.spec_from_file_location("caps", root / "src" / "pmjc" / "capabilities.py")
caps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caps)

NOT_POSSIBLE = [
    "Sending a message **as the user** from their own Discord, Slack, Messenger, WhatsApp, SMS or iMessage account: "
    "services forbid it or have no API. Use a bot account, or `pmjc_handoff` so the user sends it with one tap.",
    "Reading the user's personal DMs or full message history: only notification previews on the pmjc PC, or what a bot can see.",
    "Sending email: pmjc only creates drafts.",
    "Phone calls, reading SMS/iMessage, payments and purchases.",
    "Clicking, typing or submitting in a browser: `browser_read` is read-only.",
    "Writing or deleting files through `files_*`.",
    "Looking at images: tools return URLs; pmjc doesn't pass images to models yet.",
    "Delegating a task to another, more capable agent on the user's machine (`pmjc_delegate`): deferred, see the roadmap.",
    "Anything on a device that isn't the pmjc PC and doesn't show up in a connected service.",
]

out = ["# What pmjc can and can't do", "",
       "Generated from `src/pmjc/capabilities.py` by `scripts/gen_capabilities.py`; edit the catalog, not this file. "
       "Sessions get the same data live from `pmjc_capabilities(task)`, which also says what is connected right now.", "",
       "Pick the **most native route**: your client's own abilities first (if it can act as the user), then pmjc's tools, "
       "then pmjc's workarounds.", ""]
for c in caps.CATALOG:
    out += [f"## {c['task'][0].upper() + c['task'][1:]}", "",
            f"- **pmjc:** {c['pmjc']}",
            f"- **Needs:** {', '.join(c['needs']) or 'nothing (built in)'}",
            f"- **Limits:** {c['limits']}",
            f"- **Better if your client can:** {c['native']}",
            f"- **Workaround:** {c['workaround']}", ""]
out += ["## Not possible", ""] + [f"- {x}" for x in NOT_POSSIBLE] + [""]
(root / "docs" / "CAPABILITIES.md").write_text("\n".join(out), encoding="utf-8", newline="\n")
print("wrote docs/CAPABILITIES.md")
