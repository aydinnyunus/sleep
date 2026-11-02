<div align="center">

# Sleep

**Give your coding agents useful memory without giving memory control.**

Capture while agents work. Consolidate when they stop. Promote only what you approve.

[![Tests](https://img.shields.io/badge/tests-177%20passing-c4f042?labelColor=111827&style=flat-square)](#install)
[![Python](https://img.shields.io/badge/python-3.10%2B-369eff?labelColor=111827&style=flat-square)](https://www.python.org/)
[![Runtime dependencies](https://img.shields.io/badge/runtime%20dependencies-0-ffcb47?labelColor=111827&style=flat-square)](#install)
[![Agents](https://img.shields.io/badge/agent%20targets-9-a78bfa?labelColor=111827&style=flat-square)](#supported-agents)

</div>

Sleep adds a reviewable memory cycle to nine selectable coding-agent targets. It records useful observations during a session, turns them into provisional memory after the session, and keeps permanent memory behind an explicit human approval step.

<p align="center">
  <img src=".github/assets/sleep-replay.png" alt="Terminal showing the sleep cycle: agents capture observations, sleep consolidates them into provisional memory, and only your promote makes them permanent" width="920" />
</p>

<p align="center">
<em>The whole loop in one screen: agents write disposable captures (T0) while they work, <code>sleep</code> distills them into provisional memory (T1) that your next session can use, and nothing becomes permanent (T2) until you run <code>promote</code>.</em>
</p>

## What you get

- **Automatic capture.** Hooks and the Pi extension collect useful observations while you work.
- **Budgeted consolidation.** Sleep runs only after the record and time gates pass.
- **Review before permanence.** New memories stay provisional until you promote or refuse them.
- **Nightly dream cycle.** Mine repeated preferences, consolidate recent observations, and measure memory effects while you are away.
- **Measured replay.** Run the same task with memory off and on before deciding whether a memory helps.
- **Repeated preference detection.** `mine` finds instructions you gave in multiple sessions without a model call.
- **Project isolation.** A project's provisional memory is injected only into that project.
- **Reviewable permanence.** Each approved memory in your index carries its review date and source, so you can tell when a saved instruction was pinned.
- **Relevance-ranked injection.** With a query, provisional memory is ordered by overlap with the live request and each line carries its age, so the most relevant record survives the `--max` cut and stale facts read as stale.
- **Stale-value scan.** `doctor --stale <value>` finds which approved records still hold a value you know has changed (for example a deployment URL). Unreviewed provisional candidates are also flagged: a fact the agent proposed once and never rediscovered for 4+ weeks keeps getting injected into every session's context, so `doctor` surfaces it as `stale-candidate-unrediscovered` (a report line it never acts on, nothing is dropped).
- **Safety gates.** Secrets are redacted, hostile text is quarantined, and machine-generated prompts cannot claim user provenance.

## Install

Requirements:

- Python 3.10 or newer
- Claude Code CLI for model-backed consolidation and replay
- One or more supported coding agents from the table below

```bash
./install.sh --dry-run     # show every change without writing
./install.sh               # install skill + hooks; asks whether to add the
                           # nightly pass and the Pi/OmO extension (no flags needed)
./install.sh --extension   # also install the Pi/OmO extension (no prompt)
./install.sh --nightly     # also schedule the nightly pass (no prompt)
./install.sh --launchd     # alias for --nightly (macOS convention)
```

On Windows the entry point `./install.sh` becomes `python tools\install.py` (or
`py tools\install.py`); the flags are identical:

```bat
py tools\install.py --dry-run
py tools\install.py --extension --nightly
```

**Which agents get the capture hooks is your choice.** A plain `install.py` on a
real terminal asks which coding agents to wire (multi-select, default Claude Code).
Scripts and CI can pick explicitly without a prompt:

```bash
python3 tools/install.py --agents claude                 # Claude Code only (default)
python3 tools/install.py --agents claude,gemini,cursor # several verified hook formats
python3 tools/install.py --agents pi,oh-my-pi          # extension targets stay independent
SLEEP_PROJECT=/path/to/project python3 tools/install.py --agents opencode
```

Accepted keys are `claude`, `gemini`, `pi`, `oh-my-pi`, `cursor`, `opencode`,
`windsurf`, `codex`, and `cline`. The interactive prompt shows the same list.
Selecting one key never installs another key. The older `--extension` flag selects
`pi` only.

| Key | Surface written | Capability | Confidence |
|---|---|---|---|
| `claude` | `$CLAUDE_HOME/settings.json` | Prompt capture, tool capture, stop and compact consolidation | Verified |
| `gemini` | `~/.gemini/settings.json` | `BeforePrompt`, `AfterTool`, `SessionEnd` | Measured hook grammar |
| `pi` | `~/.omo/agent/extensions/sleep` | Full TypeScript extension capture and context injection | Verified |
| `oh-my-pi` | `~/.omp/agent/extensions/sleep` | Pi-lineage extension path | Best effort |
| `cursor` | `~/.cursor/hooks.json` | Tool capture and session-end consolidation | Documented native hooks |
| `opencode` | `<project>/.opencode/` | Tool capture and idle consolidation through a project plugin | Verified plugin grammar |
| `windsurf` | `~/.codeium/windsurf/hooks.json` | Prompt and code-write capture | Measured hook grammar |
| `codex` | `~/.codex/config.toml` | Completion notification only; no per-turn prompt capture | Documented, limited |
| `cline` | VS Code user `settings.json` | `request_created` capture only | Best effort |

OpenCode is project-scoped. Set `SLEEP_PROJECT` when the project is not the
current directory. The installer renders the repository path into the copied
plugin and registers `./.opencode/plugins` in that project's
`.opencode/opencode.json`.

Codex has no per-turn hook. Its `[notify]` integration can wake consolidation
when a run completes, but it cannot capture the prompt stream. If `[notify]`
already owns a different command, Sleep leaves it unchanged. Cline and Oh My Pi
are marked best effort because their public integration contracts are less stable.
A missing event should be reported with the affected agent, config path, and the
last lines from `~/.sleep/logs/`.

Amp, Antigravity, GitHub Copilot, Aider, and OpenHands are not installer targets.
Their available surfaces are respectively absent/unstable lifecycle hooks or
transcript/event-stream polling, which this installer does not impersonate as a
working command-hook integration.

The installer runs the test suite first, creates backups before configuration
changes, and is safe to run again. `--dry-run` does not modify existing links or
config files. Uninstall removes only exact Sleep-owned commands, preserves foreign
entries, and backs up JSON/TOML files before changing them. It is one Python
script and works on macOS, Linux, and Windows; per-platform scheduling is chosen
automatically:

- **macOS** - a LaunchAgent (`~/Library/LaunchAgents/com.aydinnyunus.sleep-nightly.plist`).
- **Linux** - a cron job (`crontab -l`, kept inside owned `# BEGIN/END aydinnyunus-sleep-nightly` markers).
- **Windows** - a Task Scheduler task via `schtasks /create /tn aydinnyunus-sleep-nightly`.


### Pi / OmO and Oh My Pi extensions

Pi and OmO require Sleep's user-installed extension for automatic integration:

```bash
./install.sh --extension
```

The installer links this repository's `extension/` directory to the selected
extension target only:

```text
pi        -> ~/.omo/agent/extensions/sleep
oh-my-pi -> ~/.omp/agent/extensions/sleep
```

Use `--agents oh-my-pi` for Oh My Pi. `--extension` remains an alias for selecting
`pi`; it does not install Oh My Pi implicitly.

The extension loads on the next Pi or OmO session and performs three jobs:

- captures user messages and relevant tool activity into T0,
- injects matching T1 provisional memory into the agent context,
- wakes the sleep policy after the agent settles.

Verify that it loaded:

```bash
omo -p 'hi' >/dev/null 2>&1
tail -4 ~/.sleep/logs/extension.log
```

The log should contain `session_start`, `publish`, and `agent_settled` entries.

Without this extension, the Sleep CLI still works when you run commands manually, and Claude Code can still use its own hooks. Pi and OmO, however, will not capture sessions, inject provisional memory, or trigger sleep automatically.

### Install from npm (Pi / OmO)

Sleep also ships as the npm package `pi-sleep-agent`, so Pi and OmO can use

the extension without cloning the repository. Install it through the `pi install`

package flow:

```bash
pi install npm:pi-sleep-agent
# or, for a specific published version
pi install npm:pi-sleep-agent@0.1.0
```

The package contains the extension (`pi.extensions`), the Sleep skill

(`pi.skills`), and the Python runtime behind it, so the CLI works from the

installed package too. Pi manages the package under its own directory; do not

guess that path. The extension resolves its own runtime from the package and

records it in the log, so the verification below proves it loaded the installed

copy rather than a checkout:

```bash
omo -p 'hi' >/dev/null 2>&1
tail -4 ~/.sleep/logs/extension.log     # session_start, publish, agent_settled
```

Those three log lines are the proof that the packaged extension loaded and its

Python runtime ran. If they do not appear, reload the extension (`pi install` it

again, or restart the session); do not dig under the package directory and guess

a path.

Verify the tarball contract locally without publishing:

```bash
npm run pack:check     # what npm pack would ship, as JSON
npm run smoke:package  # install the tarball into an isolated Pi home and prove it loads
```

To remove Sleep and restore the backed-up configuration:

```bash
./uninstall.sh
```

On Windows, `python tools\uninstall.py` (or `py tools\uninstall.py`).

## Quick start

Check the installation:

```bash
./sleep.py status -v
./sleep.py doctor
./sleep.py sleep --dry-run
```

Capture a useful observation manually when needed:

```bash
./sleep.py capture   --text "The staging gateway requires X-Route: blue."   --kind fact   --source user
```

Run a consolidation pass:

```bash
./sleep.py sleep --dry-run
./sleep.py sleep --force
```

Review the resulting candidate before making it permanent:

```bash
./sleep.py status -v
./sleep.py show <candidate-id>
./sleep.py promote <candidate-id> --dry-run
./sleep.py promote <candidate-id>
```

Reject an incorrect candidate without deleting its audit trail:

```bash
./sleep.py refuse <candidate-id> --note "Incorrect for the current deployment."
```

## Day-to-day workflow

After installation, normal sessions need no extra commands:

1. The integration captures observations into the temporary T0 buffer.
2. The sleep gate waits for at least 3 records and 6 hours since the previous pass.
3. A model condenses the buffer into provisional T1 candidates.
4. Provisional candidates can appear as clearly marked background context in matching projects.
5. You inspect candidates and choose whether to promote them into permanent T2 memory.

```text
session activity -> T0 buffer -> gated sleep -> T1 provisional memory
                                                   |
                                             review or replay
                                                   |
                                             explicit promote
                                                   |
                                             T2 permanent memory
```

## Dream cycle

Dream is Sleep's unattended reflection phase. It does not update model weights and it never writes permanent memory on its own.

A dream pass performs four steps:

1. **Mine.** Find instructions you repeated across separate sessions in the same project. This step is deterministic and makes no model call.
2. **Consolidate.** When the sleep gate passes, ask the configured model to turn recent T0 observations into provisional T1 candidates.
3. **Hygiene.** Run the read-only `doctor` scan and fold any findings (duplicate headings, near-duplicate or contradicting candidates, records parked in quarantine awaiting review, or stale values when you pass `--stale <value>`) into the night's report.
4. **Replay.** When replay is due, run selected tasks with provisional memory off and on, then write a comparison report.

Every result stays reviewable. Candidates remain in T1 until you explicitly run `promote`.

Preview the complete dream pass without model calls or writes:

```bash
./sleep.py nightly --dry-run
./sleep.py nightly --dry-run --stale https://old.internal/app   # also scan approved memory for a changed value
```

Run it now:

```bash
./sleep.py nightly --force
```

Run the two dream components separately when you want more control:

```bash
./sleep.py mine --dry-run
./sleep.py mine

./sleep.py replay --limit 3              # show the replay plan
./sleep.py replay --run --limit 3 --budget 0.10
```

`--budget` is the maximum cost per task across both replay arms. Dream reports are written under `$SLEEP_HOME/reports/` and never trigger promotion automatically.

## Useful commands

| Command | Purpose |
|---|---|
| `./sleep.py status -v` | Show buffer, candidate, quarantine, and permanent-memory status |
| `./sleep.py sleep --dry-run` | Check whether the sleep gate would run |
| `./sleep.py sleep --force` | Run consolidation now |
| `./sleep.py show <id>` | Inspect a candidate and its rendered context |
| `./sleep.py promote <id>` | Move an approved candidate to permanent memory |
| `./sleep.py demote <id> --note "..."` | Un-approve a promoted memory: mark the approved record as retracted (kept on record, not deleted) and reopen the candidate as refused |
| `./sleep.py refuse <id> --note "..."` | Record that a candidate is incorrect |
| `./sleep.py replay --run --limit 3` | Compare selected tasks with memory off and on |
| `./sleep.py mine --dry-run` | Find preferences repeated across sessions |
| `./sleep.py nightly --dry-run` | Preview the unattended nightly workflow |
| `./sleep.py doctor` | Find duplicate or overlapping memory records, flag contradicting candidates (same subject, one side carries a correction marker), flag provisional candidates not rediscovered for 4+ weeks, and report records parked in quarantine awaiting a human (report only, never dropped, merged or un-parked) |
| `./sleep.py doctor --stale <value>` | Show which approved records still hold a stale value (e.g. a changed URL) |
| `./sleep.py supersede <old> <new>` | Mark `<old>` invalid as of now and `<new>` valid as of now (`valid_until` / `valid_from`), so the window a memory was true is readable from the files |
| `./sleep.py inject --project "$PWD"` | Print provisional context for the current project, newest first, each line dated |
| `./sleep.py inject --project "$PWD" --query "..."` | Same, but rank records by overlap with the live request so the most relevant survive `--max` |

Run `./sleep.py <command> --help` for command-specific options.

## Supported agents

The installer support matrix and confidence levels live in [Install](#install).
All selected integrations share `$SLEEP_HOME`, so candidates reviewed from one
client are visible to another when the project scope matches. Capture depth is not
identical: Claude, Gemini, Pi/OmO, OpenCode, Cursor, and Windsurf expose multiple
lifecycle boundaries; Codex is completion-only; Cline is request-created-only;
Oh My Pi is a best-effort Pi-lineage extension path.

## Configure unattended model calls

Interactive runs inherit your current environment. Scheduled macOS runs read additional values from `$SLEEP_HOME/env`, which defaults to `~/.sleep/env`.

```bash
mkdir -p ~/.sleep
touch ~/.sleep/env
chmod 600 ~/.sleep/env
```

The file accepts `KEY=VALUE` lines. Common settings are:

```text
ANTHROPIC_API_KEY=...
ANTHROPIC_BASE_URL=...
ANTHROPIC_MODEL=...
SLEEP_MODEL=...
```

An explicit `--model` wins over `SLEEP_MODEL`, which wins over `ANTHROPIC_MODEL`. Existing process environment variables win over values in the file.

Before enabling the schedule, test the same path manually:

```bash
./sleep.py nightly --dry-run
./sleep.py sleep --force --budget 0.10
```

## Review and safety model

Sleep separates memory into three tiers:

| Tier | Meaning | Who writes it |
|---|---|---|
| T0 | Temporary session observations | Hooks, extension, or `capture` |
| T1 | Provisional memory shown as unverified background | Consolidation and `mine` |
| T2 | Permanent memory | Only an explicit `promote` command |

Additional safeguards:

- Model output is always stamped as agent-generated and cannot mark itself user-confirmed.
- Provisional memory is rendered as background, not as an instruction.
- Secret-like values are replaced with redaction markers.
- Prompt-injection and exfiltration shapes are moved to quarantine.
- Mixed-project buffers are partitioned by project and consolidated separately; unaddressed records stay in T0.
- Replay reports results but never promotes memory automatically.
- Every model call has an explicit budget limit.

## Data and logs

All runtime data lives under `$SLEEP_HOME`, which defaults to `~/.sleep`:

```text
buffer/         temporary T0 records
candidates/     provisional T1 records
reports/        replay reports
logs/           hook and nightly logs
.quarantine/    rejected unsafe input
state.json      sleep and replay timestamps
env             optional scheduled-run environment
```

Useful logs:

```bash
~/.sleep/logs/hook-fires.log
~/.sleep/logs/hook.log
~/.sleep/logs/nightly.log
```

Use another home for testing or for a separate memory pool:

```bash
SLEEP_HOME=/tmp/my-sleep ./sleep.py status -v
```

## Troubleshooting

**Nothing was captured**

```bash
./sleep.py status -v
./sleep.py doctor
```

Check `~/.sleep/logs/hook-fires.log`. If it is empty, the integration did not fire. If it contains events but the buffer is empty, the events were ignored or quarantined.

**Sleep keeps skipping**

The default gate requires 3 records and a 6-hour interval. Inspect the decision with:

```bash
./sleep.py sleep --dry-run
```

Use `--force` only when you intentionally want a model call.

**The scheduled pass cannot reach the model**

Confirm that `~/.sleep/env` exists, has mode `0600`, and contains the same provider settings used by your interactive shell. Then run `./sleep.py sleep --force` before relying on the scheduled pass (launchd / cron / Task Scheduler).

**A candidate appears in the wrong context**

Run `./sleep.py show <id>` and check its `cwd`. Mixed-project batches are partitioned by canonical `cwd`; each project is consolidated independently, while records without a project address stay in T0.

## More documentation

- [Sleep: the reviewable-memory extension for coding agents](https://aydinnyunus.github.io/2026/11/01/sleep-agent-memory-extension/) (blog)
- [Measured evidence and validation history](docs/EVIDENCE.md)
- [Sleep Framework analysis](sleep-framework.md)
- [Shared `/sleep` skill](shared-skill/sleep/SKILL.md)
