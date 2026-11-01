---
name: sleep
description: Use Sleep's reviewable T0 to T1 to T2 memory cycle in Pi or OmO. Use when the user asks to capture a durable observation, run sleep or dream, inspect provisional memory, measure replay, or promote an approved candidate.
---

# Sleep for Pi and OmO

The installed Sleep extension sets `SLEEPCTL` to the packaged Python entry point. Run full operations with:

```bash
python3 "$SLEEPCTL" <command>
```

If `SLEEPCTL` is empty, the extension did not load. Ask the user to install or reload `@aydinnyunus/pi-sleep`; never guess a checkout path.

## User-facing command

The package registers `/sleep`:

```text
/sleep status
/sleep capture <text>
/sleep sleep [force]
/sleep mine [dry-run]
/sleep nightly [dry-run|force]
/sleep review
/sleep show <candidate-id>
/sleep promote <candidate-id>
/sleep refuse <candidate-id>
/sleep replay [run]
/sleep doctor
```

Use `/sleep` when the user is driving the workflow. Use `python3 "$SLEEPCTL"` when the agent must inspect structured output or pass advanced flags.

## Memory tiers

- T0 is temporary session capture under `$SLEEP_HOME/buffer/`.
- T1 is provisional background under `$SLEEP_HOME/candidates/`.
- T2 is permanent memory and requires the user's explicit approval through `promote`.

Never promote automatically. Replay is report-only. A mined preference stays provisional. Current user instructions always outrank provisional memory.

## Safe operating sequence

```bash
python3 "$SLEEPCTL" status -v
python3 "$SLEEPCTL" sleep --dry-run
python3 "$SLEEPCTL" nightly --dry-run
python3 "$SLEEPCTL" show <candidate-id>
python3 "$SLEEPCTL" promote <candidate-id> --dry-run
```

Only run the final `promote` without `--dry-run` after the user explicitly approves that candidate.
