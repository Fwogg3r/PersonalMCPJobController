# Thinking level benchmark (2026-11-11)

**Question:** what do lower thinking levels cost a local reasoning model in tool use and agentic work, and how
should pmjc pick a level per request?

**Setup:** Qwen3.8 27B (GGUF) in LM Studio on the owner's PC, `thinking.control: param` (top-level
`reasoning_effort`). Six variants:
- `off` (none), `low`, `medium`, `high`, and `xhigh` (the template's default) as fixed levels;
- `adaptive` (the shipped rules with high mapped to xhigh).

Two suites ran overnight, sequentially, on one GPU:
- **Smoke:** `pmjc eval smoke`, 78 prompts × 1 run per variant (468 runs), against the real connectors with acts recorded.
  The prompts are 65 generic ones plus 13 of the owner's own.
- **Eval:** `pmjc eval run --set basic --set multistep -n 2` with fake connectors and scripted approvals (228 runs).

No LM Studio engine errors in either suite.

## Results

Smoke (78 runs per level):

| level | mean score | perfect | median s | p90 s | mean turns | reasoning tokens/run |
|---|---|---|---|---|---|---|
| off | 0.844 | 55 | 15.3 | 39.0 | 2.96 | 0 |
| low | 0.825 | 55 | 24.0 | 85.7 | 3.27 | 463 |
| medium | 0.801 | 52 | 27.4 | 97.9 | 3.73 | 579 |
| high | 0.838 | 55 | 25.8 | 87.1 | 3.46 | 482 |
| xhigh | 0.808 | 53 | 23.8 | 85.8 | 3.51 | 528 |
| adaptive | 0.857 | 58 | 25.9 | 70.3 | 3.22 | 429 |

Eval (38 runs per level):

| level | mean score | perfect | median s | p90 s | mean turns | reasoning tokens/run |
|---|---|---|---|---|---|---|
| off | 0.956 | 33 | 8.4 | 23.7 | 3.21 | 0 |
| low | 0.928 | 30 | 22.9 | 79.4 | 4.13 | 547 |
| medium | 0.927 | 29 | 23.5 | 77.4 | 4.08 | 645 |
| high | 0.932 | 29 | 18.8 | 56.1 | 3.61 | 463 |
| xhigh | 0.911 | 30 | 24.9 | 81.3 | 3.89 | 580 |
| adaptive | 0.965 | 34 | 23.7 | 55.9 | 3.79 | 536 |

Per set (eval), score and median seconds:

| set | off | low | medium | high | xhigh | adaptive |
|---|---|---|---|---|---|---|
| basic | 0.95 / 6s | 0.92 / 14s | 0.95 / 16s | 0.95 / 13s | 0.94 / 16s | 0.97 / 15s |
| multistep | 0.96 / 16s | 0.94 / 31s | 0.89 / 33s | 0.90 / 28s | 0.88 / 38s | 0.96 / 29s |

## What it says

1. **For tool use, thinking buys nothing measurable on this model.**
   - Every fixed level scores within noise of `off`: about ±0.03, with one run per smoke task and two per eval task.
   - No set improves monotonically with the level. Multistep is not better with thinking (off 0.96, xhigh 0.88).
   - Tasks that differ across levels differ in both directions, which is the signature of run-to-run noise. Examples:
     - places nearby: off 0, low 1, high 0;
     - Spotify seek: off 1, low 0.5, medium 1.
2. **Thinking costs time.**
   - Median latency is 1.6–1.8× off on the smoke test and 2.2–3× off on the eval. p90 is 2.2–3.4× off.
   - Thinking runs also take more turns: 3.5–4.1 against 3.0–3.2 with off. The model second-guesses and re-checks.
   - Each run carries 430–650 reasoning tokens that the user waits for.
3. **The shipped adaptive rules were the best-scoring variant (0.857 / 0.965), but not by a significant margin.** It
   started Spotify at low 20 times out of 25 and multistep at high or medium 14 times out of 16. It paid almost the full
   latency of thinking.
4. **Most failures don't depend on the level.** They were harness or test problems; fixed or recorded below.

## Failures that didn't depend on the level, and what was done

| failure | cause | fix |
|---|---|---|
| All 13 owner prompts (home, roadmap, API spend) | The smoke home never copied `connectors.d/` and `plugins/`, so the owner's own connectors didn't exist | `live_home` copies both (definitions only, no secrets) |
| Model said "Home Assistant isn't configured" while a plugin offered `home_status` | `pmjc_capabilities` only searched the static catalog | `tool_select.live_matches`: the result lists connected tools that match the task first and loads them (phase6/p) |
| "Like / add to playlist / move the song that's playing" | Nothing was playing overnight; refusing was correct | Prompts name a song, or say it's fine if nothing plays |
| Geocode the Eiffel Tower | The model knew the answer without a tool | A plain street address |
| Research finished in 6 turns (limit 5) | Turn limit too tight for research plus answer | Limit 6 |
| Add or remove a playlist song, write a note after listing the vault | Taint: reading the owner's own Spotify or notes marks the run untrusted, then the egress act is refused | Design question, recorded as phase8/q |
| Handoff to the dentist | The model asked for the dentist's number first | Reasonable; left as is |

## Design decision

- **Thinking is a cost to justify, not a default.** For a tool-calling harness on this class of model, start at `off` and
  think only for requests that read like building, investigating or analysing (and for `/think` or a strong route).
- **Express it per model through `thinking.levels`.** The pmjc levels stay a difficulty scale, so connector hints
  and rules stay generic. A model whose thinking does help tool use (a larger model, or another template) keeps the
  identity mapping.
- **Candidate for this model ("lean", validated below and not adopted):** `levels: {low: none, medium: none, high: medium}`. Only high-rated runs think
  from the start. A medium-rated run (several connectors or steps) that hits a failure signal escalates to high and
  starts thinking. A low-rated run that escalates stays at none.
- **Not measured:** coding and long investigations.
  - Neither suite has a build or debug task, so `high → medium` for those is a judgement, not data.
  - A coding smoke set is the next measurement (phase6/n).

The validation run (off against lean, after the fixes above) is below.

## Validation (off vs lean)

Same suites, after the fixes, 2 variants: smoke 78 × 1 per variant, eval 38 per variant.

| suite | variant | mean score | perfect | median s | p90 s | mean turns | escalations |
|---|---|---|---|---|---|---|---|
| smoke | off | **0.893** | 63 | 16.6 | 42.5 | 2.95 | 0 |
| smoke | lean | 0.868 | 59 | 18.0 | 80.0 | 3.38 | 10 |
| eval | off | **0.963** | 33 | 10.8 | 27.5 | 3.55 | 0 |
| eval | lean | 0.930 | 30 | 12.6 | 57.5 | 3.21 | 4 |

- **The fixes helped more than any thinking level did.** Off went from 0.844 to 0.893 on the smoke test. Owner prompts
  went from 0.58 to 0.73, and places from 0.25 to 1.00.
- **Lean was no better than off and slower at the tail.** Its p90 was twice off's. Runs that did think spent about 900
  reasoning tokens. Multistep scored 0.90 against off's 0.96.
- **Final recommendation for this model:**
  - Use `thinking: {control: param, policy: fixed, default: off}`.
  - `/think` at the start of a request still turns thinking on for that one request.
  - Revisit with a coding smoke set, and for any other model.
- **Still failing at every level:**
  - The taint cases (phase8/q): playlist add and remove, note write and append after a listing.
  - Unsave and transfer, which proposed nothing.
  - Research and playlist create, which ran over their turn limits.
  - The handoff, which asks for details first.
  - The eval's `thread_reply_by_mail`, which is the phase8/p reply-after-reading case.
