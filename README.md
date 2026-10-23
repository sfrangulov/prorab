# prorab

A Claude Code skill that turns one Claude Code session into a **foreman** (*prorab*) running
**crews**: other Claude Code sessions, each in its own herdr pane and git worktree, working on
several projects at once by **work orders**. The foreman plans the work with you, issues work
orders, answers the crews' questions within each order's scope and budget, accepts or returns
their results, watches context, usage limits and spend, and keeps working for hours or overnight
while you are away. It writes a morning report and leaves every decision that is yours
(merging, pushing, raising a budget) to you.

The foreman itself executes nothing: every change to code or documents goes to a crew.

## What it does

- **Work orders and stages** in a journal (a [beads](https://github.com/steveyegge/beads)
  database kept in the office, the clone of this repository): goal, stages, stop points, a dollar
  estimate at API prices, clearances for hazardous work, acceptance criteria.
- **Crews** started in herdr panes, one worktree each; work orders delivered and confirmed on
  screen; questions answered on your behalf from the work order and the journal.
- **Acceptance** against facts: the crew's report is checked against the worktree, the commits
  and the heavy runs it claims; failed reports go back with the list of problems.
- **Scheduling under limits**: the 5-hour and weekly usage limits, the shift budget and each work
  order's estimate decide what may start; heavy runs share a small number of slots so the Mac
  stays usable.
- **Context control**: a crew near its context threshold hands over to a fresh session with a
  written handover; the foreman hands over itself the same way.
- **Night shifts** with a morning reserve of the 5-hour window, an emergency stop, and a push
  notification only for the few events that cannot wait.
- **Gates**: anything only you can decide (a consent prompt, a login, more budget, a matter of
  taste) stops that work order with a recorded question; the rest goes on.

It triggers on requests to hand out work across projects, open crews or start a night shift
(in English or Russian), or on `/prorab`. It is not meant for ordinary work inside one project.

## Requirements

- macOS (a shift keeps the Mac awake with `caffeinate`; tested on macOS only).
- [herdr](https://herdr.dev) ≥ 0.9.5 (the terminal workspace for agents); the foreman runs inside a herdr pane (`HERDR_ENV=1`).
- [bd (beads)](https://github.com/steveyegge/beads) ≥ 1.3 for the journal.
- Claude Code. The [superpowers](https://github.com/obra/superpowers) plugin is recommended: crews
  follow its process (brainstorming, plans, test-driven development, verification, review). Without
  it the foreman appends a fallback procedure to each work order.
- `python3` with the standard library only: nothing to `pip install`.

## Install

The journal lives in `~/projects/prorab/.beads` by default, so clone the repository there (or set
`PRORAB_BEADS_DIR` to another `.beads` directory):

```bash
git clone <repository-url> ~/projects/prorab
```

### Project-scoped (default)

The repository already holds `.claude/skills/prorab`, a relative link to `../../prorab`. Start
Claude Code inside a herdr pane in the clone, which becomes the foreman's office, and ask for
crews or type `/prorab`:

```bash
cd ~/projects/prorab
claude
```

Nothing outside the clone is touched.

### Global (only if you want it)

To have the skill in every session, link it into your skill folders yourself. This is your
choice; the foreman never makes these links on its own.

```bash
mkdir -p ~/.agents/skills ~/.claude/skills
ln -s ~/projects/prorab/prorab ~/.agents/skills/prorab
ln -s ~/.agents/skills/prorab ~/.claude/skills/prorab
```

Undo: `rm ~/.claude/skills/prorab ~/.agents/skills/prorab` (the links only; the clone stays).

### Optional: the limits sensor

Without the sensor the foreman reads usage from the status lines and the limit messages and uses
default dollar rates. The sensor taps Claude Code's status line and gives exact 5-hour and weekly
percentages, reset times and a measured rate. It edits `statusLine` in `~/.claude/settings.json`
and keeps a timestamped backup beside it; the foreman offers it once and installs it only on your
explicit yes.

```bash
python3 prorab/scripts/sensor.py install         # shows the planned change, changes nothing
python3 prorab/scripts/sensor.py install --yes   # installs it
python3 prorab/scripts/sensor.py status          # installed / fresh
python3 prorab/scripts/sensor.py uninstall       # restores the previous statusLine
```

### Optional: crew hooks

On your yes the foreman starts crews with a hooks settings file kept in its cache (never in your
config). The hooks block in code the commands the safety rules forbid, report events without
reading screens and enforce the emergency stop. Turn them on or off with
`python3 prorab/scripts/journal.py kv set hooks.enabled 1` (or `0`).

## Safety rules, in short

- The foreman answers **no consent prompt** (trust, MCP servers, imports, bypass warning, API key,
  tool permission) and never logs in: those are yours, and the work order waits for you.
- **No push, pull request, release or merge into main** without a work order that quotes your
  words. End-of-branch choices go to the morning report.
- Your installed skills and configs stay untouched, except the sensor with your consent. The
  projects' own beads databases are only read.
- Limits, budget and estimates stop new work; "ignore limits and finish" lifts them for one goal
  and never lifts the safety rules or the machine protection.
- Crews work in your permission mode (usually bypass), so the rules are enforced by the work
  orders, acceptance and, once enabled, the crew hooks.
- Emergency stop: `touch ~/.cache/prorab/STOP`; release: `rm ~/.cache/prorab/STOP`.

## Tests

```bash
python3 prorab/tests/run.py --fast   # fast tier: everything except the tests that run the real bd
python3 prorab/tests/run.py          # full suite (needs bd; uses temporary journals only)
python3 prorab/tests/run.py prorab.tests.test_crew   # one module
```

The runner runs groups in parallel, bounds each one in time and kills a stalled group with a
stack dump. Tests never touch your real `~/.claude`, `~/.cache/prorab`, the office journal or
herdr panes.

Two guard tests keep the repository clean: `test_language` (English terms, no Cyrillic outside a
short allowlist) and `test_no_private_data` (no home-directory paths, no e-mail addresses, and
none of the words in `PRORAB_PRIVATE_WORDS`, a comma-separated list that is never stored in the
repository):

```bash
PRORAB_PRIVATE_WORDS="word1,word2" python3 -m unittest prorab.tests.test_no_private_data
```

## Layout

```
prorab/SKILL.md          the foreman's rules
prorab/references/       work orders, acts, the customer, superpowers, defaults.json, live notes
prorab/scripts/          journal, crews, monitor, scheduler, limits, slots, shift, sensor, hooks
prorab/tests/            unit tests, fixtures, the bounded parallel runner
prorab/evals/            trigger and pressure evaluations
docs/superpowers/        the design spec and the implementation plan
```

## License

MIT — see [LICENSE](LICENSE).
