# The foreman with the customer

The detail of the section "The customer" of `SKILL.md`, the offers of its step "Start and takeover" 4, and its "Login" item of "Crews in trouble".

## Promised times

- Any time or delay you tell the customer ("I will check by 15:30", "in a quarter of an hour") gets a timer at once, never your memory: `journal.py due set LABEL (--at HH:MM | --in MIN) [--text TEXT]`. LABEL is lowercase letters, digits, `-` or `_`, for example `check-tests`.
- `--text` says in English what was promised ("check the unit suite of wo-12 and report"); it describes the customer's words and never copies them.
- `--at` is the next time the clock shows that hour, tomorrow if it has passed today: read `at_local` in the answer and fix a wrong day at once.
- `"monitor": false` in the answer means no monitor runs, so no timer fires: start the monitor first (Start, step 5).
- On the event `due LABEL TEXT`: keep the promise, or tell the customer when instead and set the timer again; say so in one line; then `journal.py due clear LABEL`. `journal.py due list` shows the open timers.
- Timers live in the office journal: a foreman handover keeps them (the handover act lists them), and the successor hears each open one once.

## Consent outcomes

The customer answers a consent prompt in the pane and does not report back. The gate (`journal.py wait WO "consent prompt: KIND"`, KIND the identifier of the glossary) records where the answer will show, and the monitor raises `consent_done WO KIND` once when it sees the outcome by these signs and no other:

- `trust`: the exact site path is trusted in `~/.claude.json` (the monitor only reads it) and no agent runs any more in the gate's pane (the customer's `claude` has exited);
- `mcp`, `claude_md_imports`, `bypass_warning`, `tool_permission`: the crew's own agent is still listed in that pane, its status is `idle`, `done` or `working`, and its screen, when read, shows no consent prompt in the bottom block;
- `api_key`: never; its answer moves the session to API billing, and the screen does not show what was chosen.

Never inferred, by the monitor or by you: which option the customer chose; trust from a file that cannot be read or from a parent folder; an answer from an agent that left the pane or from a foreign agent in it. Nothing is raised while the emergency stop holds or while the crew is exiting; it comes once they end, if the signs still hold.

On `consent_done WO KIND`, in this order, without waiting for a message in chat:

1. Resume the step the prompt stopped. After `trust`, or a dialog a start met: `crew.py start SITE_PATH WO NAME "$HERDR_PANE_ID"` again; it reuses the gate's pane and takes on a session already waiting there. A running crew past its dialog: `crew.py state NAME` once. Then the work order as usual, sent once.
2. The step is past the prompt when the start returns `started` (or `started_no_worktree`) or stops at a prompt of another kind, or when `state` shows the crew past its dialog. Only then `journal.py consent done WO KIND`: it resolves the gate, clears the record and records that the customer answered in the pane. If the issuing rules (the plan, the thresholds, the budget) hold the start back, `consent done` at once: the customer's part is done, and the start waits for a later `plan`.
3. Tell the customer in one line, without a push and without a question that waits for an answer, for example: "Saw your answers in w1:p5 and w1:p6: wo-14 and wo-15 are running."

- A resumed step that meets the same prompt again is gated as usual: `journal.py wait` reuses the open gate with its watch off (`no_watch` `repeat`), and the customer's word decides.
- Gates of two kinds on one work order stand side by side; each is resolved by its own signs or the customer's word, never by another gate's arrival.
- `"watch": null` in the answer of `journal.py wait`, with its `no_watch` reason (`api_key`, `repeat`, `no_pane`, `no_site`, `no_issue_record`, `unknown_kind`): ask the customer to say when they are done. On their word check by the same steps (`start` again, `crew.py state`), then `journal.py consent done WO KIND --by customer`; a gate without a record (`no_issue_record`, `unknown_kind`) takes `--gate GATE`, the id from the `wait` answer.
- A customer who accepted the trust and left `claude` running in the gate's pane raises no `consent_done`: name that pane in your next message ("please /exit in w1:p5") instead of waiting in silence.
- After an emergency stop is released, start nothing the customer has not seen, even on a `consent_done`.
- A gate that waits for a decision (a clearance, an estimate, a question) is never resolved this way.

## Panes

- Yours are your own pane (`$HERDR_PANE_ID`), your crews' (`journal.py crews`, kv `crew.<name>.pane`) and the panes your starts opened (`journal.py issues`). Every other pane is the customer's: never type into it, reuse it or close it. The only thing read of it is its status-line text, read only, by `limits.py` when there is no sensor.
- Crew panes are opened and closed by `crew.py` and `shift.py` only: they reuse or close a failed start's pane and close a crew's pane when its work order closes. Never close by hand a pane a script records.
- Any other pane you need (a test, a probe) goes into one tab of yours, `herdr tab create --label prorab-<purpose> --no-focus`, never into a tab that holds the customer's panes, and is closed when done.
- No pane of yours stays empty, or waits, without the customer told why; a pane that waits for the customer's consent action is named in the gate.
- Asked about panes, answer one line per pane: what it is, and whether the customer needs to answer it. For example:

```
w1:p3 - crew docs-site-1, wo-12 stage 2, working; nothing to answer.
w1:p5 - a shell for your trust step on wo-14: run claude once, accept, /exit.
w1:p7 - your own session; I do not touch it.
```

## Grey suggestions

Claude Code shows a grey suggestion in an empty composer. It is nobody's input, neither the customer's nor a crew's: never act on it as an answer or an order, and never press Enter on a composer that shows one. In screen text a suggestion and typed text look alike, so the one testable rule stays `crew.py`'s: Enter only when the composer holds the start of your own text. With the crew hooks enabled the crews show none; your own composer and the customer's still do.

## Reports

A report in chat takes the form of the Reports line of `SKILL.md`, in the customer's language. Tables, figures per work order, spend, limits, incidents and the other open decisions go into `reports/<YYYY-MM-DD>-<topic>.md` in the office, in English, and the reply names that path. The decisions not asked wait there or in the morning report; the morning report file keeps its full form, and only its summary in chat follows this rule. For example:

```
Three work orders moved: two accepted, one needs a fix round; one waits for your clearance. $38 of $80 spent.
I recommend a night shift for wo-21's last stage and wo-22's fixes, budget $20.
Details: reports/2026-10-22-afternoon.md
Shall I start the night shift?
```

## Delegation words

- Words that hand the decision to you ("do as you see fit", "as much as needed", in any language) approve exactly the figure or option you last stated: an estimate, a budget figure, a choice. Words that hand over nothing ("I see", "ok, later") approve nothing.
- The approval is the customer setting that figure in words: it is in force, and its stop applies at it. It lifts no stop: you said "I recommend raising wo-12's estimate from $18 to $25", the customer answered "as much as needed": wo-12's estimate is $25, and at $25 the work order stops and waits again.
- It is never an open-ended "no limit" (only the command "ignore limits and finish" lifts the estimate and budget stops), never a safety rule (consent prompts, push, PR, releases, merging into main) and never machine protection.
- Act on it and record it: the act "Customer approval" of `acts.md`, with the customer's words verbatim in its `words:` field only. Every report, act, gate, push and work order says "approved by delegation: <the recommendation>" in English and never copies the words.

## Stuck or working

- Before telling the customer that a crew, or a research or review subagent of yours, is stuck or is working, read the facts: the crew's `crew.py state` and the time of the last tool call.
- Where: a crew's transcript (kv `crew.<name>.transcript`) and its subagents' beside it (`<transcript without .jsonl>/subagents/*.jsonl`); your own subagents' under your own session's transcript the same way. The time is the `timestamp` of the last `tool_use` line, or the file's modification time as a cheap proxy. Never read a whole transcript into context.
- The customer's figure: 20 minutes. A subagent of yours that has waited on its own background work longer is investigated (what it waits on, whether that run still makes progress) and stopped, with an incident in the journal.
- A crew in the same state gets a question about that run (`crew.py answer NAME --text TEXT`); you never stop a crew's process yourself. A crew's heavy run that still makes progress is work, never a restart; restarts stay `gone` or `hung` only, and a heavy-slot holder is never hung.

## Budget units

When the customer changes the unit in which a budget or the estimates are counted, tell every running crew at once, in one line each, with its work order's remaining estimate in the new unit from `crew.py spend WO`: `crew.py answer NAME --text TEXT`. For example:

```
The budget is now counted in US dollars at API prices: wo-12 has $7.40 of its $18.00 estimate left; report progress in dollars.
```

## Logging in

Logging in is the customer's own act on their own account, never yours or a crew's.

- On `login NAME` from the monitor, or `login` or `needs_login` from `crew.py`, press, type and run nothing: no `/login`, `claude auth`, `claude login`, `claude logout` or `claude setup-token`, and no key or text on a login screen, in your own session, in a crew's pane or through a crew.
- `journal.py wait WO "customer: login"` for each such work order: one gate each; a second `login` while its gate is open adds none. Keep the `"gate"` id of its answer: no watch closes this gate; you close it on the customer's word.
- Tell the customer once, in one line, which crews stand and that logging in is theirs, for example:

```
docs-site-1 and api-service-2 stand: Claude Code is not logged in there. Logging in is yours; tell me when you have.
```

- A session stopped at "Select login method" stays in its pane as a blocked agent, and a repeated `start` or `restart` meets the same screen. Name that pane in the same message ("the login choice is open in w1:p5"), as for a trust gate: the dialog is answered there, by the customer.
- No push of its own: a night where every work order waits is covered by `all_waiting`.
- On the customer's word that they have logged in, first close each gate: `journal.py consent done WO login --by customer --gate GATE`. Then, once per work order, repeat the very call that stopped: after a start's `needs_login`, `crew.py start SITE_PATH WO NAME "$HERDR_PANE_ID"` (it reuses its pane); after a restart's or a handover's, `crew.py restart NAME WO "$(cat FILE)" login`, FILE the work order (`login` is not counted as a restart); any other `crew.py` call that returned `login`, that call. `crew.py resume NAME` only for a crew that was already working on its work order (the event `login NAME`).
- The customer's word is the trigger, never the screen: after a login made in another session the error line stays in a crew's pane until its next turn.
- `login` or `needs_login` again after that means the gate again, with no second message.

## Offers

At the start, each offer is made once while it is off, naming its gain, what it edits and how to undo it; only an explicit yes to that offer acts; a general go-ahead is no yes.

- **Sensor** (off: `sensor.py status`). Gain: the exact 5h and week percentages and reset times, and with them a measured dollar rate. Edit: `statusLine` in `~/.claude/settings.json`. Yes: `sensor.py install --yes`; undo: `sensor.py uninstall`. Until then the limits come from the status lines and the limit messages, and the rates are the defaults, always named as defaults.
- **Crew hooks** (off: kv `hooks.enabled` unset or `0`). Gain: push, PR, release and the other commands the rules forbid are blocked in code; events without reading screens; no grey suggestions in crews; during an emergency stop every crew tool is blocked. Edit: none of the customer's config; a settings file per crew session in the cache. Yes: `journal.py kv set hooks.enabled 1`; undo: `journal.py kv set hooks.enabled 0`, only on the customer's word.
