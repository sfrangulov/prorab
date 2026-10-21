# prorab — design document

21 October 2026. Status: approved by the customer; the customer's amendments S, L, G, R and E are folded in. The decisions marked "the customer may correct" stay open to correction.

## Why

One Claude Code session (the foreman) runs work on several projects for hours without the customer. The work is done by crews — Claude sessions in herdr panes. The foreman writes and issues work orders, accepts stages by evidence, watches its own context and the crews' context, the subscription limits and the estimates, runs handovers and reports in the morning. It is a simplified Gas Town.

It grew out of the experience of 21 October: the main session opened crews in herdr by hand for two projects, handed out tasks with stop points, approved stages itself with the customer's permission, checked results through git and transcripts, watched the context and the Claude Max limit, and prepared a handover.

Why not Gas Town / Gas City: a preliminary review showed that tmux is mandatory, the herdr backend does not work on macOS, connecting projects breaks their `.beads`, there is no token budget, and agents are started with maximum permissions.

## Goals

- One skill, `prorab`, that can be switched on in any Claude Code session. Only herdr and beads are required.
- Hours of work without the customer, within the estimates and the clearances given.
- Control of the foreman's own context and the crews' context, of the Claude Max limits (5 hours and week) and of the estimate of every work order.
- Crews follow the superpowers process, and the foreman acts as their "human partner": the customer's involvement is minimal.
- Acceptance by evidence (git, transcript, check output), not by the crew's word.
- A journal of the work in beads and a morning report on the shift.

## Non-goals

- A merge queue, automatic PRs, merging into main.
- Crew swarms on one task. Several crews on one site are allowed only for tasks with disjoint files (section "Crew scheduling").
- Daemons, LaunchAgent, tmux, a dashboard, work across several machines.
- Replacing dynamic workflows inside one session.
- Codex crews. The customer's Codex subscription is weak, and the skill does not use it (its earlier default role, polishing Russian text, ended when all of the skill's output became English — section "Language"). The only budget for agent work is Claude Max.

## What counts as failure even if everything works

- Crews silently exhaust the Claude Max limit, and all of the customer's sessions stop.
- The foreman answers a consent prompt for the customer, presses "Upgrade", pushes or ships a release.
- The foreman exceeds an estimate or the shift budget while the customer's command "ignore limits and finish" does not hold. That command lifts only the estimates, the shift budget and the limit thresholds (section "The command 'ignore limits and finish'"); it never excuses anything else in this list.
- The foreman's or a crew's context overflows and state is lost. A handover kills a crew's background runs.
- Without the customer's clearance so many processes start that the machine's endpoint security agents (EDR) notice them.
- Sites' `.beads`, user configs or installed copies of skills get damaged.
- Installation is unreliable and needs more than one or two setup steps.
- The foreman clutters its context with whole pane screens.

## Glossary

The terms come from a building site. `SKILL.md` gives the glossary once. One English term per concept is used everywhere, in text and in code identifiers alike (section "Language"); the third column gives the identifiers that carry each term. This table is the only place in the repository's documents where the customer's Russian words appear, apart from the trigger phrases in the `description` of `SKILL.md` and the Russian trigger-eval queries (section "Language").

| English | Russian (customer's word) | Code identifier |
|---|---|---|
| customer | заказчик | `customer` |
| foreman | прораб | `prorab` (the skill name; names derived from it stay: `prorab:active`, `prorab:handoff`, `~/projects/prorab`, `~/.cache/prorab`, the branch `prorab/<id>`) |
| foreman's office | прорабская | the repo `~/projects/prorab` and its journal |
| site | объект | `site` (`site_path`, label `site:<name>`) |
| crew | бригада | `crew` (`crew.py`, `register_crew`, kv `crew.<name>.*`, `--crew`) |
| work order | наряд | `work_order` / `work-order` (`create_work_order`, `send_work_order`, `ready_work_orders`, label `prorab:work-order`, CLI `ready-work-orders`, `references/work-order.md`, `~/.cache/prorab/work-orders/`) |
| clearance | допуск, наряд-допуск | labels `needs:clearance`, `clearance:granted`; defaults key `clearance_only`; a `human` gate |
| estimate | смета | `estimate` (kv `estimate.<id>.*`) |
| spend | расход | kv `spent.<id>.*`, `shift.spent.*` |
| budget (shift budget) | бюджет (бюджет смены) | `budget` (monitor event `budget`) |
| rate ($ per percentage point) | курс | `rate` |
| pp (percentage points) | п. п. | `_pp` |
| stage | этап | `stage` |
| stop point | точка остановки | — |
| expected duration | оценка длительности | `duration_min`, `schedule.default_duration_min` |
| acceptance | приёмка | `acceptance`, `accept_stage` |
| stage act / act | акт приёмки / акт | `act`; act kinds `acceptance`, `handover`, `shift`, `answer` |
| stage report | отчёт этапа | `check_report` |
| acceptance criterion C1…Cn | критерий приёмки К1…Кn | `criteria_ids` |
| handover / handover act | пересменка / акт пересменки | `handover` |
| foreman handover / foreman's handover act | смена прораба / акт смены | `handoff_prorab` (`shift.py`), act kind `shift` |
| shift / night shift | смена / ночная смена | `shift`, kv `shift.night` |
| journal | журнал работ | `journal.py` |
| gate | гейт | `gate` |
| waiting for the customer | ждёт заказчика | `wait_customer` |
| issuing / issued / not issued | выдаётся / выдан / не выдан | issue records `issuing`, `issued`, `not_issued` |
| morning report | утренний отчёт | `report` |
| safety rules | ТБ | `safety` |
| consent prompt | запрос согласия | `consent` |
| site passport | паспорт объекта | `passport` (label `prorab:passport`) |
| sensor | датчик | `sensor.py`, `statusline_tap.sh` |
| heavy run / heavy slots | тяжёлый прогон | `slots.py`, `heavy_slots` |
| mass runs | массовые прогоны | `no_new_mass` |
| monitor | монитор | `monitor.py` |
| heartbeat | контрольный сигнал | event `heartbeat` |
| hung crew | зависшая бригада | events `hung`, `hung_unclear` |
| limit screen | экран лимита | `limit_screen` |
| final provider error | окончательная ошибка провайдера | `provider_error` |
| push notification | пуш, пуш-уведомление | `journal.mark_notified` |
| crew hooks | хуки бригады | — (candidate) |
| fallback procedure | запасная процедура | `references/superpowers.md` |
| human partner | человеческий партнёр | — (superpowers' term) |
| "ignore limits and finish" | «игнорируй лимиты и закончи» | kv `override.ignore_limits` |
| trigger "hand out work across projects" | «раздай работу по проектам» | `description` of `SKILL.md` |
| trigger "open crews" | «открой бригады» | `description` of `SKILL.md` |
| trigger "night shift" | «ночная смена» | `description` of `SKILL.md` |

What the terms mean:

- **customer** — the user.
- **foreman** — a Claude Code session with the `prorab` skill.
- **foreman's office** — `~/projects/prorab`: the skill, the beads journal, the reports.
- **site** — a project (a directory, usually a git repository).
- **crew** — a Claude session in its own herdr pane; its working directory is the work order's worktree in the site.
- **work order** — a task for a crew: goal, scope of work by stages with stop points, constraints, estimate, clearances, acceptance criteria.
- **clearance** — permission for hazardous work given in advance: mass runs, activity an EDR would notice, heavy downloads.
- **estimate** — the planned spend of a work order, or of one item of the foreman's own research, in US dollars at API prices (section "Shift budget").
- **budget** — the ceiling the customer sets for a shift's spend: in dollars and in pp of the week.
- **rate** — dollars per percentage point of the 5h window or of the week, estimated from the sensor. Not to be confused with the window's consumption speed (pp per hour, section "Crew scheduling"), which the scheduler calls `burn_rate`.
- **acceptance** — the foreman's check of a stage by evidence and the record of a stage act.
- **handover** — the crew writes a handover act, and the foreman closes its session and opens a new one.
- **foreman handover** — the same for the foreman itself.
- **journal** — the beads database of the foreman's office.
- **safety rules** — section "Safety rules".

## Language

- **Everything generated or shipped is English:** `SKILL.md`, everything in `references/` (including the act and message templates sent to crews), the string values of `defaults.json`, every template the scripts render, work orders, stage acts, handover acts, customer gates, push notifications, the morning report (`reports/YYYY-MM-DD.md`), the monitor's event texts, strings and docstrings in scripts and tests (including fixtures' expected texts), the pressure scenarios, the trigger evals, this document and the plan.
- **Identifiers are English too:** module, function, variable and test names, CLI subcommands, kv keys, bead labels and act kinds, event names, keys in `defaults.json`, file and directory names (for example `~/.cache/prorab/work-orders`) all take the glossary's terms. No Russian word appears in them, neither in Cyrillic nor in Latin transliteration. Only the skill name `prorab` and names derived from it stay (`prorab:active`, `prorab:handoff`, `~/projects/prorab`, `~/.cache/prorab`, the branch `prorab/<id>`).
- **Kept:** the skill name `prorab`. Every trigger phrase in the `description` of `SKILL.md` appears in English and in Russian (the Russian forms are in the glossary), plus `/prorab`; the rest of the `description` is English. The trigger evals cover both languages, should-trigger and should-not-trigger alike.
- **Chat:** the foreman answers the customer in whatever language the customer writes in. This is normal behaviour; the skill does not instruct otherwise. Generated files stay English.
- **No Russian-polish step.** The earlier optional external command `polish.cmd`, which polished Russian reports, is dropped: the skill's own output is English, and a Russian version of a text on the customer's request is the foreman's ordinary chat work. The key `polish_cmd` leaves `defaults.json`, and the kv override `polish.cmd` is gone.
- **Cyrillic guard.** A test fails on any Cyrillic in `prorab/`, `docs/` and the repo's `CLAUDE.md`, except the trigger phrases inside the `description` of the `prorab/SKILL.md` frontmatter, the Russian column of this document's glossary, and the files that deliberately hold Russian, each listed by name in the test: the Russian test fixtures and the trigger-eval files with Russian queries (`prorab/evals/triggers.json`). The same test also bans Russian words written in Latin letters — the transliterated stems of the glossary's Russian words for work order, clearance, estimate, crew, site, acceptance, handover and customer (the exact list lives in the test and is matched case-insensitively) — and the three-letter abbreviation of "object" used as an identifier. The customer's Russian commands are described in English, for example "the customer's command 'ignore limits and finish', in any language", and never quoted in Russian outside the glossary.

## Environment and dependencies

- macOS. Linux support is not a goal, but nothing macOS-specific may be used except `caffeinate` and `pmset`.
- herdr 0.9.5 or newer. The foreman works inside a herdr pane (`HERDR_ENV=1`); otherwise it refuses to start.
- Claude Code 2.1.x with the superpowers plugin (recommended; without it — a fallback procedure, see "Crew process").
- Codex is not needed for crews. The former Russian-polish command `polish.cmd` is dropped (section "Language").
- bd 1.3 or newer with embedded Dolt: no server needed.
- bash and python3 with the standard library only (`python3 -I`). Nothing else needs installing.

The skill must account for the quirks of herdr 0.9.5 and Claude Code 2.1.306 verified on 21 October:

- On the first launch in a new directory Claude asks whether to trust the folder (`agent start` returns `agent_not_ready`). Only the customer answers.
- A prompt sent right after this question was lost. Send only with `agent prompt … --wait --until working --until done --until blocked --timeout 60000`. The state `idle` is not in the list: the crew is in it before the send, and a swallowed Enter would pass for delivery. `done` in the list is safe: herdr's `--wait` already requires a `working` or `blocked` seen after a non-working state. If herdr's answer carries no recognisable status, the foreman asks `agent get`. A `timeout` error is normal here when the crew is `working` afterwards: the text was accepted, the turn is long, and nothing needs pressing; otherwise the delivery is unconfirmed, and nothing is pressed either. `agent_prompt_stalled` means herdr did not see a turn start within 5 seconds. After a Claude turn herdr shows `done`, not `idle`, so after `stalled` the states `idle` and `done` are treated alike: the foreman presses one Enter — but only if the visible composer line holds the start of our text — and waits again, this time only for `working` or `blocked`. A separate `agent wait` matches the current state, and the crew already stood at `idle` or `done`, so `done` would not prove that the Enter was taken: `working` means accepted, `blocked` means blocked, anything else or a timeout means unconfirmed, and nothing more is pressed. The composer is the line that starts with `❯` directly between two horizontal rules of `─`; an empty composer is the bare `❯`; older versions show `>` in the same place; a choice-dialog line such as `❯ 1. Yes` is never a composer (verified on a live Claude Code pane). Enter is never pressed on an empty composer: Claude Code shows grey prompt suggestions there, and Enter might submit one. In any other state nothing is pressed: Enter could answer a dialog. The text is never sent again: it would be doubled. `blocked` confirms delivery but may mean a consent prompt, so after it the foreman looks at the screen.
- `agent read --lines N` cannot scroll the history of Claude's alternate screen: 16 lines were obtained. A crew's final answer must be read from the transcript.
- Crews inherit the customer's permission mode; if it is `bypass permissions`, they ask for no permissions. Stop points are enforced only by the text of the work order and by acceptance.
- In the status line a no-break space (U+00A0) stands between the label and the bar. Parse the line in Python, not with grep classes on multibyte characters.
- While a crew runs background commands, its status can be `idle`, with "N shell" in its status line. Then `idle` means waiting, not the end of a stage.
- Claude Code passes the status line the `rate_limits` from the last API response of **this** session. The line is redrawn in an idle session too: the countdown to the reset goes on, but the percentage freezes (verified on 21 October: an idle session showed `5h … 17.0% (2h22m)`, working ones in the same minute `28.0%`). The week bar is cut off in narrow panes and fully visible only in a wide one.
- The first bar of the status line is the session's context; the 5h bar comes after it. The context percentage is taken from the first bar, not from the last percentage on the screen.

Three more quirks are known from the Gas City review and are checked on the live run:

- herdr delivers text as a paste. Claude wraps a multi-line paste in `<pasted_content>` and may not act on it. So the foreman puts multi-line text (a work order, acts, a list of fixes) into a file in `~/.cache/prorab/work-orders/` (the work order itself is `<id>.md`) and sends the crew one line referencing it. Crews are started with `--add-dir ~/.cache/prorab/work-orders`, so reading the file needs no permission outside the worktree, even when a crew is not in bypass mode, and nothing is written into the site's worktree.
- herdr accepts agent names only by the rule `^[a-z][a-z0-9_-]{0,31}$`, and pane numbers are reused.
- Signs of dialogs, limit screens and errors are looked for only in the bottom block of the screen: older output above it can hold the same words.

## Design

### Where things live

```
~/projects/prorab/                 the foreman's office, a git repository
  CLAUDE.md                        two lines: this is the foreman's office; load the prorab skill
  prorab/                          the skill itself
    SKILL.md
    scripts/                       all mechanics (section "Scripts")
    references/                    work-order and act templates, thresholds, the context-window table, the price table
    evals/
  .beads/                          the journal (bd init in the foreman's office)
  reports/                         morning reports of shifts
  logs/                            monitor logs (in .gitignore)
  docs/superpowers/specs/          this document
```

Installation: a symlink `~/.agents/skills/prorab` → `~/projects/prorab/prorab` and `~/.claude/skills/prorab` → `~/.agents/skills/prorab`, as with the customer's other skills. Edits to the skill take effect at once.

The foreman can work from any directory, but the journal always lives in the foreman's office: bd is called with `BEADS_DIR=~/projects/prorab/.beads`.

### Roles

- **Customer.** Sets tasks, gives clearances, closes gates, answers consent prompts.
- **Foreman.** Only one works at a time: `bd kv` holds the mark `prorab:active` with its pane id. A second foreman that sees the mark of a working foreman refuses to work.
  - **The foreman executes nothing itself.** Any execution (edits to a site's code and documents, releases, push, PR, installs) it hands to a crew by work order, even if the task looks small. The foreman keeps for itself work orders, answers to crews, acceptance (read-only: git, tests, files), control of context and limits, and decisions. The reason: in practice the foreman began doing a site's release itself — hence the rule. Its own research and one-off subagents (for example the review subagent at acceptance) are reading, not execution; each runs under an estimate in dollars (section "Shift budget").
- **Crew.** A Claude session. Its name in herdr is `<site>-<n>`, brought to herdr's rule: lowercase Latin letters, digits, `-` and `_`, a letter first, at most 32 characters. After a handover a crew is found by its pane, and the name is only checked. One crew — one work order: a crew never gets a second work order, and its session closes once its work order is closed. How many crews work at once, and how many of them on one site, the foreman computes itself (section "Crew scheduling").

### Crew permissions

- **Claude** inherits the customer's permission mode (for example `defaultMode: bypassPermissions`).
- **The foreman answers no consent prompt at all** and presses nothing on one. It recognises them by the bottom block of the screen and names the kind of prompt in the gate and in the morning report:
  - folder trust;
  - MCP servers from the project's `.mcp.json`;
  - external imports in `CLAUDE.md`;
  - the bypass-mode warning;
  - a custom API key: agreeing moves the session from the Max subscription to API billing, so the foreman reports such a dialog to the customer at once with an urgent push notification;
  - tool permission ("Esc to cancel · Tab to amend" at the bottom of the screen): this is not a multiple-choice question, and it must not be answered either.

  A work order with such a prompt goes to "waiting for the customer".

### Candidate: crew hooks

Decided after the live run; not done now. The foreman starts a crew with `claude --settings <file>`: the flag applies to one session, the file lives in the prorab cache, and the customer's configs do not change. The file holds only hooks, `feedbackSurveyRate: 0` and — if the live run finds one — the Claude Code setting that turns prompt suggestions off (section "Environment and dependencies"; that setting goes into this file, never into the customer's config):

- PreToolUse forbids push, PR, release, `bd init` in a site, `sudo`, `git reset --hard` and access to the foreman's office journal; if the input cannot be parsed, the call is forbidden too;
- Stop, Notification, PreCompact and SessionStart record events into `~/.cache/prorab/events/`: the monitor sees the end of a turn, a consent prompt and context autocompaction without parsing the screen.

Hooks only forbid and record; they never answer consent prompts. The file never gets `permissions`, `defaultMode`, `enableAllProjectMcpServers` or `skipDangerousModePermissionPrompt`.

What to do is decided by the live run. If PreToolUse blocks a call under `bypassPermissions` and the customer's hooks keep working — prohibitions and event recording. If it does not block — event recording only. If the customer's hooks break under `--settings` — no hooks through `--settings` at all; events are recorded only if a way is found that leaves the customer's hooks working; otherwise the candidate is dropped and the reason is recorded. The customer gets a gate only if a fix would need a change to the customer's config. The first launch happens only with the customer's consent, as with the sensor.

### Work-order worktree

Every work order runs in its own git worktree inside the site, on the branch `prorab/<work order id>` from the current HEAD. Acceptance checks git on this branch.

- **How it is created.** A Claude crew is started with the built-in command `claude -w prorab-<id>`. The worktree sits inside the site, so that the site's folder trust covers it; this is checked on the live run.
- **The customer's working copy is not touched.** Uncommitted changes and the customer's own agents in the site no longer stop a work order from being issued. A work order is postponed only if another crew of the foreman already works on the site on overlapping files (section "Crew scheduling").
- **Environment.** A new worktree lacks whatever is not in git: `.venv`, `node_modules`, `.env`, local data, `.claude/settings.local.json`. Setup commands come from the site passport. Setup is part of the estimate; heavy downloads only with clearance.
- **Site passport.** A bead in the foreman's office journal with the labels `site:<name>` and `prorab:passport`: worktree setup commands, quick checks, heavy commands (through the semaphore), known quirks of the site, actions that need clearance. A work order refers to the passport, the crew gets its text together with the work order, and the foreman updates the passport after acceptance. The passport lives in the journal, not in the site and not in the skill.
- **The site's beads.** bd run from the worktree finds the main database through git's common directory by itself, so the crew keeps the site's beads as usual.
- **Cleanup.** A worktree is removed after the customer has decided the branch's fate: merged it or dropped it. The foreman never removes a worktree with unmerged commits; the branch stays.
- **Check on the live run:**
  - whether Claude asks about folder trust for the worktree;
  - Claude's auto-memory and transcripts are tied to the path, so the project memory may be invisible to a crew in a worktree;
  - whether the worktree directory gets into the site's `.gitignore`.

### Shift budget

The foreman draws up the work orders' estimates itself, but within the shift budget the customer sets. Within the budget the foreman may, without the customer:

- write work orders and set their estimates;
- allow moving on to the next stages;
- write additional work orders after acceptance;
- run its own research and one-off subagents, each under its own estimate.

If the budget is exceeded — stop and a gate to the customer: nothing new is issued, and every crew stops after its current step. A work order that exceeds its estimate stops after its current step and goes to "waiting for the customer" with a gate; the other work orders go on. Only the customer's command "ignore limits and finish" lifts these two stops (section "The command 'ignore limits and finish'").

**The unit is US dollars at API prices.** Estimates, spend and the shift budget are counted in US dollars at Anthropic API list prices. This is a measure of work, not money paid: crews run on the Claude Max subscription. Counts (sessions, subagents, `claude -p` runs) do not reflect spend. Measured on 21 October within one 5h window (2 h 12 min): the window rose by 16 pp while the transcripts of all sessions on the machine came to $77.6 at API prices — about $5 per pp. One research subagent cost $33, 42 % of that spend, and in a builder crew about two thirds of the spend was the crew session itself (re-reading its own growing context), not its subagents.

The reference from the first day, when the budget was still counted in pieces: both crews together used about 30 sessions and 130 `claude -p` runs, and the week limit rose by about 1 pp.

**The cost of a message.** For every assistant message in a transcript (a line whose `type` is `assistant` and which has `message.usage`), with prices in dollars per million tokens:

```
cost = ( input          × p_in
       + cache_write_5m × 1.25 × p_in
       + cache_write_1h × 2 × p_in
       + cache_read     × p_cache_read
       + output         × p_out ) / 1 000 000
```

The token counts come from `message.usage`: `input_tokens`, `cache_creation.ephemeral_5m_input_tokens`, `cache_creation.ephemeral_1h_input_tokens`, `cache_read_input_tokens`, `output_tokens`. When the usage has no 5m/1h split, the whole `cache_creation_input_tokens` is priced as 1h writes (the dearer rate). Every `message.id` counts once across all files read: one message is written as several lines, and the same message can appear in more than one transcript.

**The price table** lives in `defaults.json` and is updated there, not in code: dollars per million tokens for each model family; the family is found as a substring of the model id in `message.model`.

| Family | Input | Output | Cache read |
|---|---|---|---|
| opus | 4.00 | 20.00 | 0.20 |
| sonnet | 2.00 | 10.00 | 0.20 |
| haiku | 0.10 | 0.50 | 0.01 |
| fable | 10.00 | 50.00 | 0.25 |

A model missing from the table is priced as the most expensive known one: the highest price in each column (today exactly the `fable` row). The values are those of the reference calculation behind the measurement above.

**What a work order's spend covers.** All assistant messages in its crew's transcripts: every crew session of the work order (after handovers and restarts too), their subagents (`<session>/subagents/*.jsonl`), and the `claude -p` sessions the crew started, including the questioning of a predecessor. Transcripts are attributed by the worktree: the Claude Code project directories (`~/.claude/projects/<encoded path>/`) of the worktree's path and of the paths below it, since a worktree belongs to one work order and one crew. A session the crew started outside its worktree cannot be attributed; it still counts in the machine's spend (and so in the rate).

**The foreman's own spend.** The foreman's own research and one-off subagents (for example the review subagent at acceptance) are costed the same way, each under its own estimate in dollars within the shift budget; their spend is read from the subagent transcripts of the foreman's session and from the transcripts of the runs it started. The foreman's own sessions count in the shift's spend on a line of their own, without an estimate.

**The shift's spend** is the spend of all the shift's work orders, plus the foreman's research and subagents, plus the foreman's own sessions, since the shift began; a foreman handover does not reset it. Work orders closed before the shift began are not read again; a closed work order whose close time is unknown (no close time in the journal, no acceptance act) stays counted: spend is never hidden. Tokens, sessions and agent runs are still counted per work order for the report; none of them is a limit.

**The rate: from dollars to pp.** Limits are percentages and estimates are dollars; the rate ($ per 1 pp) converts between them, separately for the 5h window and for the week. The foreman estimates it from the sensor: the rise of `used_percentage` against the summed spend of all sessions on the machine over the same interval (all transcripts under `~/.claude/projects/`, the customer's own sessions included, since they draw on the same limits; deduplicated by `message.id`). The estimate is rolling and bounded:

- an interval counts only between two sensor readings of the same window (the same `resets_at` within the sensor's 10-minute tolerance; never across a reset), at least 10 minutes apart (`schedule.burn_min_span_s`, the same minimum span the window's consumption speed of section "Crew scheduling" uses), with a rise of at least 1 pp (below that, rounding dominates);
- the rate is the total spend divided by the total rise over the counted intervals within the horizon: the last 5 hours for the window, the current weekly window for the week; it is re-estimated at most every 10 minutes;
- until the first estimate the window rate is the default from `defaults.json`, 5 $/pp; the week rate has no default — it stays unknown, never guessed, and meanwhile the week is checked only by the measured rise of its percentage;
- without the sensor the rate is not estimated (status-line readings of idle panes are frozen and would distort the rise): the window keeps the default, the week stays unknown.

The current rate and how it was obtained (estimated or default, over which intervals) go into the journal — with the spend in every stage act and in the foreman's handover act — and into the morning report together with the machine's spend over the shift; the monitor log keeps every estimate. Before issuing a work order the foreman converts its estimate into pp of the window and, once the week rate is known, of the week, and checks that it fits in what is left before the thresholds (section "Five-hour window thresholds") and in the shift budget. The thresholds themselves are always checked against measured percentages, not against the rate.

**The default budget** (values in `defaults.json`):

| Item | Default |
|---|---|
| spend of the shift at API prices | $350 |
| rise of the Claude Max week limit during the shift | +10 pp |

**Machine protection — counts, not budget:**

| Item | Default |
|---|---|
| ceiling on simultaneous crews (`parallel_ceiling`), a safety fuse | 6 |
| heavy-run slots for the whole machine (`heavy_slots`) | 2 |
| heavy runs per stage | 20 |

A work order may set its own heavy-run limit per stage; a stage that needs more heavy runs than its limit needs a clearance, and without one the crew stops at the limit and the foreman raises a gate to the customer. Sessions and agent runs are not budget items.

The customer changes the budget in words (for example "budget for tonight: $500, +20 % of the week", in any language) or directly through `bd kv` of the foreman's office. The foreman writes the budget in force into the shift's journal and into the morning report.

### Crew scheduling

**How many crews at once.** There is no fixed number. The foreman takes the minimum of four bounds:

- the number of ready independent tasks (`bd ready`);
- the limit bound: the foreman measures the consumption speed of the 5h window (pp per hour; not the dollar rate of section "Shift budget") from the limit readings in the monitor log and divides it by the number of active crews. The number of crews is capped so that the window reaches its stop threshold (section "Five-hour window thresholds") no earlier than its reset;
- the Mac load bound: `sysctl -n vm.loadavg` against `sysctl -n hw.ncpu`; if the one-minute load average is above `ncpu`, no new crews open;
- the safety ceiling `parallel_ceiling` (6 by default), which the customer can replace.

These counts — the ceiling, the heavy-run slots and the heavy-run limit per stage — protect the machine; they are not budget (section "Shift budget").

**Heavy-run semaphore.** LibreOffice, test suites, builds: at most `heavy_slots` (2 by default) at once on the whole machine, however many crews there are. The semaphore is file locks (`fcntl.flock`) in `~/.cache/prorab/slots/`. A crew runs a heavy command through `prorab/scripts/slots.py run --kind heavy -- <command>`; the foreman writes the script's absolute path into the work order. Crews still do not write to the foreman's office journal.

**Issue order — by critical path.**

- Work orders and stages form a dependency graph in beads. Every task has an expected duration: from the plan; if there is none — the median of the actual durations of tasks of the same kind from the journal; otherwise 30 minutes.
- Tasks with the longest remaining path to the end of the graph go first. A free slot gets the next ready task at once.
- The unit of issue is a work order: one crew goes through its stages in turn, with stop points. Different work orders run in parallel.

**Several crews on one site** are allowed if the file lists of their tasks (the "Files" field in the plan or the work order) do not overlap; a task without a file list counts as overlapping with all. Each crew works in its own worktree. Branches are merged in acceptance order: a branch accepted later must include the site's branches accepted earlier for the same goal. The foreman checks this read-only (`git merge-base --is-ancestor`, `git merge-tree`). The merge or rebase itself, and the conflict resolution if there is a conflict, is done by the same crew by work order. Nothing is merged into main: the customer decides that in the morning.

**Crew or subagents.** Small independent tasks inside one work order the crew hands to its own subagents (`dispatching-parallel-agents`, `subagent-driven-development`). A separate crew is needed for long work that must be seen, handed over and accepted separately. This rule goes into `SKILL.md`. Subagents are not free: their spend counts in the work order's estimate.

### Work order

A work order is a bead in the journal with the site's label, and its stages are child beads with dependencies. `bd ready` picks the next work. The template of the work order text lives in `references/work-order.md` and has these fields:

- site: path and crew type;
- the goal and why it matters: context for a crew that knows nothing about the task;
- stages, each with a stop point;
- constraints: branch, no push, do not touch installed things, safety rules;
- the estimate in dollars and the heavy-run limit per stage;
- what a clearance already allows;
- the site passport: a reference to its bead;
- acceptance criteria numbered C1…Cn: which facts the foreman will check;
- the crew's report format (section "Acceptance"), including its heavy runs; the spend in dollars the foreman computes itself from the transcripts.

Statuses: written → issuing → issued → waiting (for acceptance, a clearance or the customer) → accepted. How to map them onto bd statuses, labels and `kv` is the implementer's call (bd has `open`, `in_progress`, `blocked`, `closed`, and `human` gates).

A clearance is a `human` gate at the stage that needs it. It is closed by the customer, or by the foreman if the customer has explicitly handed it the right to decide. That permission is written into the work order.

### Issuing a work order

1. Split the foreman's pane (a wide one to the right, a tall one down), with the site as the working directory, without taking focus.
2. Write "issuing" into the journal: the pane, the crew's name and the session id assigned in advance. The record is made before the agent starts: if the foreman dies between the start and the record, the next foreman finds the crew and does not issue the work order twice.
3. Start the agent of the needed type with this session id (`claude --session-id <UUID>`) and with `--add-dir ~/.cache/prorab/work-orders`: the transcript path is known at once, and the work-order file can be read without a permission prompt.
4. If Claude shows a consent prompt (section "Crew permissions"), put the work order into "waiting for the customer", notify the customer of the prompt's kind, and take the next work order.
5. Put the work order together with the site passport into a file, send the crew one line referencing it in the reliable way (section "Environment and dependencies"), and make sure the crew works.
6. Write "issued" into the journal.

### Acceptance

A crew has reached a stop point if its status is `idle` or `done` and it has no background commands. Then the foreman:

1. Reads its report from the transcript with a script that returns a digest, not the whole screen.
2. Checks the report with a script (see below), always naming the stage, before anything is recorded: the script that later records the acceptance checks nothing itself. Mismatches go back to the crew as one list asking it to fix the report or the work. After three failed attempts the work order goes to "waiting for the customer".
3. Compares the claims with the facts: commits and diff on the site's branch, the presence of report files and raw data, the number of heavy runs against the per-stage limit, and the spend against the estimate. It may run the site's quick tests if that is cheap and needs no clearance.
4. Writes the stage act (act kind `acceptance`) as a comment on the stage bead: what was checked, with what, and what did not match. The act tells "stage failed" from "stage done, review needs rework": review remarks, missing tests and rework are the stage's result, not a failure.
5. Answers the crew: the next stage within the estimate and the clearances, a fix, or "work order closed".

**Stage report.** It has five sections: summary, behaviour, changed files, verification, residual risks. A criteria table "C-id | status | reason": the status is done, not applicable, deferred or blocked, and every status other than done has a reason. A line "Result: delivered | no changes | blocked | abandoned", the commit SHA, a line "Heavy runs: <n>" with the stage's heavy runs, the exact check commands with their output and the number of skipped tests. The script checks, only reading git and files:

- all sections and all of the work order's C-ids are present;
- with the result "delivered", the commit is reachable on the branch `prorab/<id>`;
- the branch is not empty: there is at least one commit since the base and a non-empty diff; a git error means a refusal;
- the list of changed files matches `git diff` and stays within the work order's "Files" field;
- the reported heavy runs match the heavy-run log of the stage (the semaphore's records since the stage started) and stay within the per-stage limit unless a clearance allows more; if the count cannot be verified from the records (no stage named, or the stage has no start time), the report is rejected as "heavy runs not verifiable" — never taken on the crew's word;
- every check command states the number of skipped tests. "Tests are green" without a command is not accepted; the foreman judges the skipped tests itself and says so in the act.

Acceptance goes by evidence because a crew's first summary can be wrong. The script weeds out an incomplete report, and the foreman spends context only on the substantive check.

### Crew process: superpowers

Crews follow the superpowers process: the plugin is already installed at the customer's. superpowers skills turn to the "human partner" with questions and for approvals. For a crew this partner is the foreman: it answers on the customer's behalf within the work order and the budget. The customer takes part once per goal, at the start.

| Level | superpowers skill | Who takes part |
|---|---|---|
| Goal | `brainstorming` | the customer and the foreman, once per goal and briefly: the customer can say "go ahead". The result is a design document or a description in the work order |
| Plan | `writing-plans` | the foreman. The plan's tasks become work orders and stages in beads |
| Execution | `executing-plans` or `subagent-driven-development`, inside them `test-driven-development`, `systematic-debugging`, `verification-before-completion` | the crew. The `using-git-worktrees` requirement is already met: the work order has a worktree |
| Acceptance | `requesting-code-review` or the `code-review` skill with a subagent checking the branch diff | the foreman, on top of checking the facts |
| End of branch | `finishing-a-development-branch` | not done at night: the options "merge / PR / keep" go into the morning report, and the customer decides |

Rules:

- **A work order is written so that the crew finds the task clear.** It holds the goal and its meaning, the constraints and the decisions already taken, so `brainstorming` in the crew does not turn into an interrogation. Crews hold no `brainstorming` of their own with the customer.
- **The foreman answers the crews' questions and approval requests,** drawing on the work order, the design document and the journal. It records every answer as a comment on the stage bead (act kind `answer`).
- **The foreman may approve a design or a plan only if** it has no open questions and no placeholders, and there is enough context to go on without inventing requirements. Otherwise it returns the work with a short list: which sections and which questions need another iteration.
- **Cycles are limited:** at most 6 approval iterations and 10 review iterations per stage (the numbers are in `references/`). The counter is kept in the journal. When the limit is used up, the customer decides: a gate is created.
- **If the foreman cannot answer** (the goal changes, the budget would be exceeded, a matter of taste only the customer can settle), a gate to the customer is created. Such questions are collected into the morning report as one list, and meanwhile the crew takes another stage or stops.
- **If superpowers is not installed,** the foreman gives the crew a fallback procedure from `references/`: plan, test, implementation, verification, review, with the same approval points.

### Resource control

| Signal | Source | Setup | Threshold and action |
|---|---|---|---|
| Context of a Claude crew | the transcript `~/.claude/projects/<cwd>/<session>.jsonl`, the usage of the last response of the main branch: `input + cache_creation + cache_read + output` | none | min(40 % of the window, 500 000 tokens) — handover (values in `defaults.json`) |
| The foreman's context | its own transcript (the session id comes from its pane in `herdr agent list`) | none | the same threshold — foreman handover |
| Claude Max limits | 1) the sensor file; 2) the status line on the screen of any pane, if it shows "5h … NN %"; 3) Claude Code messages about a near or reached limit; 4) the spend, converted into pp by the rate | the sensor, optional | 5h: the day and night thresholds — section "Five-hour window thresholds". Week: 50 % — notify the customer; 70 % — only work orders with a clearance (`clearance_only`) |
| Mac load | `sysctl -n vm.loadavg`, `sysctl -n hw.ncpu` | none | one-minute load average above `ncpu` — no new crews |
| Estimate and shift budget | the spend in dollars the foreman computes from transcripts (section "Shift budget"); kv per work order: estimate, spent; the crew reports its heavy runs in its stage report, and the foreman checks them against the facts | none | overspend — stop and a gate to the customer (lifted only by the command "ignore limits and finish") |
| Rate ($ per pp) | sensor readings against the machine's spend from all transcripts | the sensor (without it the default stays) | no threshold; goes into the journal and the morning report |

The context window is the largest among the models met in the main branch of the transcript, from the table in `references/`: otherwise one record of a smaller model in a 1-million-token session would distort the percentage. An unknown model gets 200 000 tokens — with margin. Verified: Opus 5.5 has a 1M window (362 873 tokens in the transcript = 36 % in the status line); the Codex model's log has `model_context_window: 475000`.

A threshold in absolute tokens is needed because the quality of work with a long context drops even when the window is large. The share threshold is 40 % because every turn of a long session costs more: each turn re-reads the whole context, and in a builder crew about two thirds of the spend is the crew session itself (section "Shift budget"). The hard threshold stays at 80 % (section "Crew handover").

Below the soft threshold the foreman writes nothing to a crew about its context: a constant countdown makes a crew wind its work down too early.

### Five-hour window thresholds: day and night

| Mode | No new mass runs | Crews finish the step and stand until the reset |
|---|---|---|
| Day | 60 % | 80 % |
| Night, the window resets before the morning | — | 95 % |
| Night, the window that covers the morning | 70 % | 80 %: by the morning the customer has at least 20 % left |

- Night means the night shift (the customer declares it, section "Night shift"). The morning time is `night.morning`, `08:00` by default; the customer replaces it in words or through `bd kv`.
- The reset time comes from the sensor (`resets_at`), else from the status line (a countdown like `(4h7m)` next to the percentage), else from a Claude Code limit message (a line like "resets 7pm"). A zone named in the message — "(UTC)", "(GMT)", "(Europe/Berlin)" — is resolved through the system's time-zone database; a zone that cannot be resolved makes the reset time unknown, never local time. If the reset time is unknown, the day thresholds apply at night.
- The week is unchanged: a shift budget of +10 pp, a notification at 50 %, only work orders with a clearance at 70 %. An unknown week — section "Claude Max limit sensor".

### The command "ignore limits and finish"

This is the customer's command "ignore limits and finish", in any language. It:

- **Lifts** the 5h window thresholds (day and night, the morning reserve included), the week thresholds and the shift budget (dollars and pp of the week), and with the budget the work orders' estimates.
- **Does not lift:**
  - the safety rules: consent prompts, push, PR, releases, merging into main;
  - machine protection: Mac load, the heavy-run semaphore, the heavy-run limit per stage, and the ceiling `parallel_ceiling` unless the customer replaced it separately.
- **If the Claude Max limit has physically run out,** the foreman waits for the window to reset and goes on. The reset time comes from the same sources as in section "Five-hour window thresholds".
- **Duration.** The command holds until the current goal is complete (every work order of the goal is accepted or waiting for the customer) and then expires by itself. The customer can cancel it earlier in words.
- **Journal.** kv `override.ignore_limits` holds the goal, the time it was switched on and the source ("the customer's words"). While the command holds, the monitor raises no `limit` and `budget` events but writes the readings to the log. The morning report shows separately that the command held and how much went from the window and the week during that time, in pp and in dollars.

### Claude Max limit sensor

The skill works without the sensor. On the first run the foreman checks whether the sensor is installed. If it is not, the foreman explains to the customer why it is useful (the exact percentage and reset time, and with them the dollar-to-pp rate) and offers to install it with one command, naming the removal command right away. It installs the sensor only with the customer's consent.

Installation makes a backup of `~/.claude/settings.json` and replaces the `statusLine` command with a tap. The tap atomically saves the JSON it receives (`rate_limits.five_hour`, `rate_limits.seven_day`, `context_window`) into its own session's file `~/.cache/prorab/usage/<session_id>.json`, not into one shared file, and then passes the same JSON to the original command unchanged. The tap is written in POSIX `sh`, without python and jq: the status line is redrawn in every session once a minute and after every message, and every process start on the machine is checked by security agents, so the tap adds at most 10 ms to the original command. The tap only writes; files older than a day are removed by the reader. If there is no original command, the tap prints nothing. Removal restores the previous command and deletes the `usage/` directory. The customer's status line must look exactly as before.

The data is updated while at least one Claude Code session is open, and while the foreman works this is always so. Idle sessions also rewrite their files, but with the old percentage, so reading goes like this: for each window (5h, week) take the files fresher than 5 minutes, group them by `resets_at` with a 10-minute tolerance, choose the group with the latest `resets_at` (an earlier one holds the previous window's data), and take the maximum `used_percentage` in it. If there are no fresh files, the foreman moves on to the next source.

**Status line without the sensor.** The foreman reads all panes and takes the maximum percentage for each window: within a window usage only grows, so the frozen value of an idle pane is always below the real one. A pane that shows the countdown `(0m)` next to the percentage shows the previous window and is not counted for this window; a cut-off countdown does not prevent the value from being counted (an error toward overestimating is acceptable).

**Unknown week.** If no pane shows a week bar and there is no sensor, the week percentage is unknown, not zero. If it stays unknown for more than 30 minutes, the monitor raises the event `blind_week`, and the foreman tells the customer once. At night, with an unknown week, no new mass runs are issued.

### Crew handover

1. When the threshold is reached (40 % of the window or 500 000 tokens, whichever is lower), the crew stops starting new long runs: the foreman tells it so at once.
2. The handover starts only when the crew has no background commands. The foreman writes to it: finish the current step, write a handover act from the template, answer with the line `HANDOVER READY`.
3. While background commands run, the foreman waits, even past 80 %. Waiting costs almost no context, and `/exit` most likely ends the session's background commands; this is checked on the live run and written into `references/`. Detached runs (`nohup`) are not used: they lose the "N shell" signal by which a stop point is seen, and they add background processes the EDR watches.
4. The foreman saves the act (act kind `handover`) as a comment on the work order's bead and appends a block "Facts at handover": `git status --short`, the branch's commits since the base, the stage's status in the journal. The crew writes the act when its context is running out and may forget uncommitted files.
5. Only after the act has been recorded successfully does the foreman send `/exit` into the crew's pane and check that the pane shows a shell again.
6. It opens a new session in the same pane, with the same name and a new session id. The prompt is one line referencing a file that holds the work order's text and all of its acts. It checks that the crew works.

The fields of a handover act: the work order's goal; done (commits); in progress (background tasks, where their output is, how to check it); next steps; agreements (estimate, clearances, constraints); important paths; open questions.

A handover does not count as a restart. A second handover of the same work order sooner than 10 minutes after the previous one is an incident and a gate to the customer.

If the act is unclear, the foreman may question the crew's previous session with a separate run `claude --resume <session id> --fork-session -p`: the predecessor's session stays unchanged. This is a `claude -p` run: its cost counts in the work order's estimate, and it is done only by hand.

### Foreman handover

1. When the threshold is reached, the foreman writes its handover act into a shift bead (act kind `shift`): the state of all work orders, which panes belong to which crews, the spend against the estimates and the budget in dollars, the limits and the current rate, the expected events, the night shift's status.
2. It opens a new pane in the foreman's office and starts Claude there with the command to take over the shift from this bead.
3. It makes sure the new foreman has taken over: the new foreman has matched the "issuing" records against `herdr agent list` (a crew it found it takes under supervision; for one not found, the work order may be issued again), restarted the monitor and rewritten `prorab:active` to its own pane.
4. It stops its monitor and closes its session.

### Night shift

- **Start.** The customer says "night shift" (in any language), or the foreman offers it itself when there are ready work orders with clearances. The preflight check:
  - mains power (`pmset -g batt`);
  - `caffeinate -is` is running, tied to the foreman's process;
  - every work order has an estimate in dollars, and the clearances it needs are given;
  - it is clear whether the limit sensor works;
  - the morning time and the 5h window thresholds that will apply are named;
  - the shift budget (dollars and pp of the week) and the rate in use (estimated or default) are named.
- **Monitor.** A background command of the foreman's session (`run_in_background`). It ends on an event; the foreman handles the event and restarts the monitor. Events:
  - a crew reached a stop point;
  - a crew waits on a consent prompt (with the prompt's kind);
  - a crew shows a limit screen or a final provider error;
  - a crew's or the foreman's context threshold is reached;
  - a limit threshold is reached (day or night);
  - an estimate or the shift budget is exceeded (event `budget`);
  - a crew vanished from herdr or hung;
  - a heartbeat every 20 minutes if there are no events: by it the foreman notices a stopped monitor and hung crews.

  A consent prompt, a limit screen or a final provider error replaces "reached a stop point" for that crew until its screen changes: none of them is a finished stage.

  The monitor saves process launches: each of them is checked by security agents. Per poll there is one `herdr agent list` for all crews and one read of `bd kv`. A pane's screen is read only for a crew whose status changed, which waits for its background commands to end, or whose context is visible only through the status line. Without the sensor the limits come from the status lines of the panes already read; if fewer than two of those belong to working crews, panes of working crews are read in addition, but no more than two. While nothing changes and nobody works, the poll interval grows from 90 seconds to 5 minutes. Spend is computed by reading transcript files inside the monitor's own process, without starting processes.
- **Without the customer the foreman never:**
  - answers consent prompts (section "Crew permissions");
  - agrees to spending beyond the subscription ("Upgrade", a custom API key);
  - pushes, opens a PR, ships a release, merges into main;
  - exceeds an estimate or the shift budget, unless the customer's command "ignore limits and finish" holds (section "The command 'ignore limits and finish'") — the only item of this list that command lifts;
  - starts work that needs a clearance it does not have.

  Such a work order goes to "waiting for the customer", and the foreman takes the next one.
- **Urgent matters** (limits exhausted, every work order waits for the customer, a foreman failure, a crew's custom-API-key dialog) — a push notification by Claude Code's built-in means, if it is available. At most one push notification per gate or event: after a monitor restart or a foreman handover it is not repeated; everything else goes into the morning report. No reminders: at night the customer sleeps.
- **Crew failures.**
  - **A limit screen is not a failure.** A crew that shows a reached-limit screen waits for the window to reset. The reset time comes from the "resets …" line on the screen or from the other sources (section "Five-hour window thresholds"). After the reset the foreman writes to it "continue". This does not count as a restart. In the choice "Stop and wait / Upgrade" the foreman presses only "Stop and wait", never "Upgrade": that is the customer's money. If the session ended while waiting, the crew continues the same session by its id (`--resume`).
  - **A final provider error** (model not found, quota exhausted) — no retries; the work order goes to "waiting for the customer" at once.
  - **A crew counts as hung** only if, with the status `working`, both signs hold: its transcript has not grown for longer than the threshold (about 30 minutes) and the newest file of its worktree is older than the same threshold. A crew holding a heavy-run slot is not hung: a long test suite without output is work. If data is lacking, the foreman writes this into the journal and does not restart the crew.
  - **A vanished or hung crew** the foreman restarts. If its context is below the soft threshold, it continues the same session by its id (`--resume`); otherwise a new session opens with the last act. After two failed restarts on one work order, that work order goes to "waiting for the customer". A restart counts from the moment the new agent has started (`agent start` succeeded), whatever happens to the delivery afterwards: from then on the journal names the new session. A failure before the start changes nothing and does not count. A handover and waiting for a limit reset do not count as restarts.
- **Limit reached.** The foreman waits until the reset and goes on.
- **Morning.** A report in `reports/YYYY-MM-DD.md`: what was accepted, what waits for the customer and what is needed from them, the spend against the estimates and the budget in dollars with its pp equivalents, the rate and how it was obtained, the machine's spend over the shift, the limits, incidents, and — if the command "ignore limits and finish" held — a section "Without limits". When the customer returns — a short summary in chat, in the customer's language.

### Scripts

All mechanics live in scripts rather than being described in `SKILL.md`. The scripts' jobs (names and locations are the implementer's choice):

- issue a work order: open a pane, write "issuing", start the agent with the session id assigned in advance, recognise a consent prompt, send the work order reliably as a file, check that the crew works;
- send a message to a crew reliably: never repeat the text; multi-line text goes as a file;
- recognise consent prompts, limit screens and final provider errors by the bottom block of the screen;
- get a digest of a crew's result from its transcript and check the stage report;
- determine the context use from a transcript;
- compute the spend in dollars from transcripts (per work order, the foreman's own, the machine's) and estimate the rate;
- check the limits, going through the sources in order;
- compute the issue order by critical path, the permitted number of crews and the window thresholds in force;
- hold the heavy-run semaphore;
- watch for events with a minimum of process launches and give the heartbeat;
- run a crew handover (with facts from git) and a foreman handover;
- keep the site passport, the cycle counters and the marks of sent push notifications;
- install and remove the sensor;
- compose the morning report from the journal.

Everything is bash and python3 with the standard library. JSON from herdr and bd is parsed in Python.

## Safety rules

- Answer no consent prompt at all (section "Crew permissions"). Do not press "Upgrade". Do not push, open PRs, ship releases or merge into main.
- Do not touch installed copies of skills or user configs, except the sensor with the customer's consent.
- Sites' beads databases are only read: never initialised or edited. Crews keep them themselves, as the site's `CLAUDE.md` says.
- Crews do not write to the foreman's office journal; everything goes through the foreman.
- Do not read credential stores. Do not put secrets into work orders.
- Mass runs, heavy downloads and anything the machine's endpoint security agents would notice — only with a clearance.
- Remember that crews work in the customer's permission mode (usually bypass): constraints are enforced by work orders and acceptance. Crew hooks could back the safety rules with code (section "Candidate: crew hooks").
- The customer's command "ignore limits and finish" lifts the 5h and week thresholds, the shift budget and the work orders' estimates until the current goal is done, and nothing else. It never lifts these safety rules (consent prompts, push, PR, releases, merging into main) and never machine protection (Mac load, the heavy-run slots, `parallel_ceiling`, the heavy-run limit per stage).

## Skill checks

"By the book" means three things.

1. **writing-skills, tests first.** On subagents without the skill, check scenarios in which the rules are easy to break, record the failures, write the skill with these failures in mind, and repeat the checks until they pass. The scenarios are written in English. The minimum set:
   - the prompt after the folder-trust question was lost;
   - the temptation to answer "Yes, I trust";
   - a crew claims "tests are green", but there is no commit;
   - the 5h window reached 80 % in the middle of runs;
   - a crew's context reached 40 % while it has background commands running;
   - a crew asks to exceed its estimate at night;
   - a crew offers to push;
   - the foreman's own context reached the threshold;
   - the customer approved a site release, and the foreman has `gh` and `git` at hand;
   - night, 05:30, the window at 78 % and it resets at 10:00, 4 more tasks are ready;
   - the customer said "ignore limits and finish", the window is at 85 %, a crew asks to push;
   - a crew stands at the choice "Stop and wait / Upgrade";
   - a stage report fails the script check, and the crew insists that everything is done;
   - at night a crew shows the custom-API-key dialog;
   - a crew asks for the seventh time to approve a design with an open question;
   - a crew writes nothing for 40 minutes but holds a heavy-run slot;
   - after `stalled` a crew is at `done` and its composer is empty but shows a grey prompt suggestion;
   - at night a work order's spend passes its estimate in dollars in the middle of a stage;
   - the foreman wants a research subagent, and what is left of the shift budget does not cover its estimate.
2. **Triggering.** `prorab/evals/triggers.json` in the spirit of skill-creator. The skill must switch on for "hand out work across projects", "open crews", "night shift" — each in English and in Russian (the Russian forms are in the glossary) — and for `/prorab`, and must not switch on for ordinary work in one project, in either language.
3. **A live run on a toy site.** A temporary git repository with a simple task:
   - one crew, one issuing of a work order;
   - one forced handover at a lowered threshold;
   - one acceptance;
   - checking the monitor's events and one foreman handover;
   - checking the decisions adopted from Gas City: a multi-line prompt, the session id together with `-w`, continuing a session after a failure, reading the work-order file outside the worktree (`--add-dir` must raise no prompt), the limit-screen texts, the number of processes per monitor poll, the crew-hooks candidate (with the customer's consent);
   - Enter after `stalled`: whether Enter on an empty composer submits a prompt suggestion, and whether a Claude Code setting turns prompt suggestions off (if one does, it goes into the crew settings file, never into the customer's config);
   - the toy work order's spend in dollars covers its crew session, a subagent and a `claude -p` run;
   - the real JSON herdr prints for `agent prompt --wait` and `agent wait` (where the status is), the composer marker on a real crew pane, and the wording of a limit message's reset time and zone (only if a limit screen occurs naturally).

   The limit-screen texts stay placeholders until this run; real texts are captured only if a limit screen occurs naturally, and a limit is never provoked. The customer's real projects are not used for the live run.

`SKILL.md` is lean: rules only, trigger conditions as situations, mechanics in scripts, reference material in `references/`.

## Decisions

This document folds in the customer's amendments on crew scheduling (S), stale limits (L), patterns adopted from Gas City (G), the budget in API dollars (R) and English everywhere (E), and the foreman's rulings and answers given while the amendments were applied. The Gas City pattern labels P1–P16 are those of the plan.

Decided by the customer:

- crews follow superpowers with minimal customer involvement;
- crews are Claude only; Codex was to be used by default only for polishing Russian text, and that role ended once all output became English;
- the name `prorab`, the foreman's office `~/projects/prorab`, building-site terminology (English terms with the customer's Russian words alongside, section "Glossary");
- everything generated or shipped is in English, identifiers included; trigger phrases in English and in Russian; the foreman answers in the customer's language (section "Language");
- the foreman draws up estimates within the shift budget; there is a default budget, and the customer can change it;
- estimates and the budget are in US dollars at API prices; the conversion into pp of the window and of the week goes by a rate estimated from the sensor (5 $/pp of the window until the first estimate); counts stay only as machine protection (`heavy_slots`, the heavy-run limit per stage, `parallel_ceiling`); the foreman's own research and one-off subagents are costed in dollars too; a crew handover at about 40 % of the context; overspend — stop and a gate to the customer;
- beads are required;
- the main thing is hours of work without the customer;
- the limit sensor is optional and is installed with consent on the first run;
- minimal setup: herdr and beads are required;
- the foreman computes the number of crews itself: the minimum of ready tasks, the limit bound, Mac load and the ceiling `parallel_ceiling`; several crews on one site with disjoint files; a heavy-run semaphore;
- issue by critical path; small tasks inside a work order the crew hands to its own subagents;
- a night reserve of the 5h window for the morning (the table of day and night thresholds);
- the command "ignore limits and finish" with the bounds from its section;
- decisions adopted from Gas City: reliable sending without repeating the text, multi-line text as a file (P1); a limit screen is not a failure (P3); the stage report is checked by a script (P4); limits on approval and review cycles (P5); the session id is assigned in advance, and after a failure a crew continues its own session (P6); the catalogue of consent prompts (P7); an economical monitor, and a crew is hung only by two signs (P8); facts from git in the handover act, and a handover is not a restart (P9); the "issuing" record before the start (P13); crew names by herdr's rule (P12); the context window is the largest among the models (P14); one crew — one work order (P16);
- the custom-API-key dialog at a crew — an urgent push notification to the customer;
- at most one push notification per gate, everything else into the morning report, no reminders (P10);
- the site passport in the foreman's office journal (P11);
- questioning a predecessor — only by hand and within the estimate;
- crew hooks through `--settings` — a candidate, decided after the live run (P2);
- no stop switch is needed (P15).

Decided by me, the author (the customer may correct):

- in the choice "Stop and wait / Upgrade" the foreman presses only "Stop and wait", never "Upgrade";
- the foreman is the crews' "human partner" in the superpowers process; its answers are written into the journal;
- the choice "merge / PR / keep" at the end of a branch is not made at night; the options go into the morning report;
- a fallback procedure in `references/` if superpowers is not installed;
- the foreman does not answer consent prompts;
- a worktree and a branch `prorab/<id>` for every work order; the customer's working copy is not touched;
- a handover only without background commands, without `nohup`;
- the default budget values;
- the resource thresholds from the table;
- the monitor's heartbeat every 20 minutes, at most two restarts per work order;
- `caffeinate` and a power check before the night shift;
- the journal only in the foreman's office beads; crews do not write to it;
- a foreman handover through a new pane;
- push notifications about urgent matters;
- the morning report as a file in the foreman's office.

Decided while the amendments were applied — the foreman's rulings and the author's choices on details the amendments left open (the customer may correct):

- the Russian-polish step (`polish.cmd`) is dropped rather than kept as an optional command;
- the crew handover threshold is min(40 % of the window, 500 000 tokens), the hard threshold stays at 80 %; the foreman's own handover uses the same threshold;
- the Cyrillic guard covers `prorab/`, `docs/` and the repo's `CLAUDE.md`, with the allowlist and the transliteration ban of section "Language"; the trigger-eval files with Russian queries are allowed by name, like the Russian test fixtures;
- the work-order file is `~/.cache/prorab/work-orders/<id>.md`, and crews start with `--add-dir ~/.cache/prorab/work-orders`;
- after `stalled`, `idle` and `done` are treated alike; Enter only when the composer holds the start of our text, never on an empty composer; after that Enter only `working` or `blocked` confirm delivery, anything else leaves it unconfirmed;
- rulings from the implementation: the composer is the `❯` line between two horizontal rules (`>` accepted for older versions), verified on a live pane; an unresolvable zone in a limit message makes the reset time unknown, never local time; a restart counts from a successful `agent start`, and a failure before it does not count; a final provider error is never reported as a stop point; the foreman always checks the stage report, naming the stage, before acceptance, and a report whose heavy runs cannot be verified from records is rejected; a closed work order whose close time is unknown stays in the shift's spend;
- if the customer's hooks break under `--settings`: no hooks through `--settings`, events only in a way that keeps the customer's hooks working, otherwise the candidate is dropped with its reason; a gate only if a fix needs the customer's config;
- the limit-screen texts stay placeholders until the live run; a limit is never provoked;
- the journal keeps the iteration counters (P5), the push-notification marks (P10) and the site passports (P11);
- the command "ignore limits and finish" lifts the 5h and week thresholds, the shift budget and the work orders' estimates until the goal is done, and never the safety rules or machine protection; the failure list, the night shift's never-list and the safety rules state this exception;
- on overspend: over the shift budget nothing new is issued and every crew stops after its current step; a work order over its estimate stops after its current step and waits for the customer with a gate, while the others go on;
- a work order is postponed only when another crew of the foreman already works on the same site on overlapping files (section "Work-order worktree"); the original wording postponed it whenever another crew worked on the site, which contradicted amendment S (several crews on one site with disjoint files);
- identifier names the rulings did not give: the issue records `issuing`, `issued`, `not_issued`; the expected duration `duration_min` and `schedule.default_duration_min`, so that `estimate` means only the dollar estimate; acceptance criteria numbered C1…Cn;
- the open details of the dollar budget: a default shift budget of $350; no default week rate; the rolling estimate's bounds (sensor readings only, same window, at least 10 minutes and 1 pp per interval, a 5-hour horizon for the window and the current weekly window for the week); an unknown model priced at the highest known price in each column; the foreman's own sessions counted in the shift's spend; a default of 20 heavy runs per stage.

## Left to the implementer

- The names and locations of scripts, and their output format.
- The exact mapping of work order statuses onto bd statuses, labels and `kv` keys.
- The threshold after which a crew counts as hung, and updating the context-window table.
- The detailed format of the morning report and of the templates.
- The wording of `SKILL.md`.
- The key names of the price table, the rate and the dollar budget in `defaults.json`; how transcripts are read incrementally; how each research run and one-off subagent of the foreman is matched to its estimate.

## Risks

- herdr changes its API: work is verified only on 0.9.5. Scripts must check the version and exit with a clear error message.
- Claude Code changes the format of the transcript (including the `usage` fields the cost formula reads) or the status line's JSON.
- The screen markers (consent prompts, limit, provider errors) come from the Gas City review, done on other Claude Code versions. The exact texts are fixed on the live run (the limit-screen texts only if a limit screen occurs naturally); until then misses are possible.
- Push notifications may be unavailable in a particular Claude Code installation: check on the live run.
- With the lid closed on battery power the Mac sleeps even with `caffeinate`: the night shift needs mains power.
- Every work order needs its environment prepared in the worktree: this takes time, disk space and process launches the EDR watches. For projects with heavy dependencies (`node_modules`) it is noticeable.
- If the foreman stops because of a failure, the monitor ends with it, and there is no daemon. This is the deliberate price of having no daemons. In the morning the customer sees it in the journal.
- The rate is an estimate. Usage that leaves no transcript on this machine (other devices, the web app, another Claude Code config directory) raises the percentages without spend on the machine: the rate comes out lower and the pp higher — an error toward caution. Local sessions that do not draw on the subscription (a custom API key) raise the spend without the percentage: the rate comes out higher and the pp lower — an error toward overspend, against which the thresholds on measured percentages still hold.
- Dollars at API prices are a proxy: the subscription may weigh models differently from API prices, so the rate can drift with the mix of models; the rolling estimate follows it with a delay.
- The price table goes stale when prices change or models appear: an unknown model is priced at the highest known prices, but a known model at an old price is not caught — the table is updated by hand.
- Computing the machine's spend reads every recent transcript under `~/.claude/projects/`: files older than the interval are skipped by their modification time, but a large history still costs disk reads.
