---
name: sleep
description: Run the memory sleep cycle - capture wake observations, consolidate them into provisional candidates, review them, and promote only what replay has measured. Use when the user asks to save what was learned, run a memory/sleep pass, review provisional memory, or asks why memory changed. Never edits existing memory files.
---

# Sleep

A three-tier memory cycle modelled on Google Research's sleep framework, translated
to an agent that cannot update its own weights. Read `~/Research/sleep/README.md`
for the mapping and the limits; this file is the operating procedure.

Entry point (no install step, no dependencies beyond Python 3):

```
SLEEP=~/Research/sleep/sleep.py
```

Every command below is `$SLEEP <verb>`; add `--help` to any verb.

## The one rule that matters

Tiers are not a formality:

| tier | where | who may write it |
|------|-------|------------------|
| T0 buffer | `~/.sleep/buffer/` | any session, any time, cheap |
| T1 candidate | `~/.sleep/candidates/` | only the consolidation pass, through gates |
| T2 canonical | the memory repo | only `promote`, only on explicit human approval |

Reaching T2 without the human saying so is the failure mode this design exists to
prevent. The user's standing rule is that memory files are not written without an
explicit request; `promote` is the only place that request is honoured, and
`source: user` must mean the user actually said it.

## Capture (T0) - during the session, always safe

```
$SLEEP capture --text "..." --kind fact|preference|procedure|warning --source agent
$SLEEP capture --kind procedure "short form: positional text, or pipe it on stdin"
```

Capture what cost time and will cost time again: a constraint, a failed attempt and
why, a tool behaving unlike its docs. Secrets are redacted on the way in and
injection-shaped text is quarantined, so capture is safe to call liberally.

## Sleep (T0 -> T1) - offline, policy-gated

```
$SLEEP sleep --dry-run         # what would happen, no model call
$SLEEP sleep                   # gated: >=3 records and >=6h since last sleep
$SLEEP sleep --force           # ignore the gate (still cost-capped by --budget)
$SLEEP distill --dry-run       # skip policy, show the prompt payload
```

The model proposes to **stdout only**: it runs read-only and has no write access
anywhere. Python parses the proposal blocks and applies redaction, quarantine,
trust ceiling and dedupe to each one, so a poisoned proposal cannot skip a check
and a run that goes wrong cannot leave a trusted file behind. Buffer pruning happens
only after a pass that actually produced proposals: a failed run leaves the
session's observations in T0 for the next attempt.

## Review and promote (T1 -> T2) - explicit

```
$SLEEP status -v               # tier census + provisional list
$SLEEP show <id>               # record + exactly how it renders into context
$SLEEP promote <id> --dry-run  # print the T2 write without writing
$SLEEP promote <id>            # writes a NEW file, appends one index line
$SLEEP supersede <old> <new>
$SLEEP doctor                  # repeated headings in one file, candidate restating memory
```

Promotion is expansion-only: it never merges into or rewrites an existing memory
file, which is what keeps git revert a complete undo.

## Mine what was repeated (layer 2.5) - no model, no cost

```
$SLEEP mine --dry-run               # sentences said verbatim in >=2 separate sessions
$SLEEP mine                         # each hit: one T1 candidate + one replay task proposal
```

Repetition across sessions, matched literally: the output is the person's own words, never a
paraphrase, and the candidate is filed at `trust=observed`, because said twice is not
confirmed. Proposals land in `~/.sleep/replay/task-proposals.json` and are never merged into
the task file; they carry no `criteria_keywords`, so one scores `not measurable` until a human
writes the keywords. Machine-written prompts - a delegate's brief, a probe ending in "then
reply READY" - are stamped at capture and skipped here, or a week of probes would read as a
preference. `nightly` runs this before the gated pass, so a night that never reaches a model
still banks what was repeated.

## Measure (layer 3) - before trusting it

```
$SLEEP replay --limit 3             # dry-run plan, no cost
$SLEEP replay --run --limit 3       # A/B with memory off vs on, report only
```
Reports land in `~/.sleep/reports/`, sandboxes in `~/.sleep/replay/runs/`.

Replay writes a report; it never promotes. If a candidate cannot be shown to help a
fixed task, it stays provisional. That is the difference between continual learning
and journaling.

## Context injection

`$SLEEP inject [--project <path>]` prints the provisional block. Provisional memory is rendered as
background with its provenance and trust, explicitly not as instructions - current
instructions always win. Do not rephrase a provisional line into an instruction for
yourself. A candidate is filed under the project its observations were captured in,
chosen by the buffer records and not by the directory the consolidator happened to run
from, so a note learned in one repo does not turn up in another. A batch containing more
than one project fails closed and stays in T0 until it can be consolidated one session at a time.

On the Pi side the extension injects this block at session start and after
compaction. On the Claude Code side it is deliberately manual: a `SessionStart` hook
would push unverified memory into every session globally, and that wiring waits for
replay to show the block helping.

## What runs by itself

With the hooks installed: `UserPromptSubmit` captures the user's own words
(`source: user`), `Stop`/`PreCompact` call `sleep` through the same gate (background,
cost-capped), and on Pi `PostToolUse` records an agent-side trace plus a gated sleep
after writes. `SLEEP_DISABLE=1` turns all of it off; the hooks print nothing and
always exit 0, so a broken sleep can never block a session. To undo:
`~/Research/sleep/uninstall.sh`.
