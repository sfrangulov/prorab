# Act texts and messages to crews

`crew.py` sends the first fenced block under the headings Handover request, Pause, Emergency pause, Continue, Changed work order, End background shells, Message by file and Fix the report; the block under Customer decision to drop the branch equals the marker `crew.DROP_MARKER`. Do not rename these headings and keep each block in its own section. The act templates show the form of each act, with every `<...>` to fill in; each section names who writes the act and the command that records it.

## Acceptance act

Written after `crew.py check_report NAME WORK_ORDER --stage STAGE` passed and the claims were compared with the facts. Put the filled block into a file and record it with `crew.py accept WORK_ORDER STAGE --act-file FILE`; `accept` checks nothing itself, appends the `Spend:` block (`journal.spend_block`: the work order's dollars against its estimate, the shift's against the budget, the rates) and closes the stage. Do not write the spend yourself. The first line is the verdict; the morning report shows it. Before the act, write the report's "For the passport" notes (`passport_notes` of the check) into the site passport; the `Passport:` line says which.

"done, review needs rework" is a result, not a failure: review findings, missing tests and follow-ups go to the next stage, a fix round or a new work order. "failed" means the evidence does not show the stage reached its stop point.

```
Verdict: <accepted | done, review needs rework | failed>
Stage: <stage id> <title>; work order <id>; branch prorab/<id> at <SHA>
Report check: crew.py check_report --stage <stage id> passed; warnings: <SKIP counts and how the foreman judged the skipped tests, or none>
Criteria:
- C<n>: <status> - <the evidence: command and output, file, commit>
Git: <commits <base>..HEAD; diffstat; files read>
Checks run by the foreman: <command - result - SKIP: <n>, or none>
Heavy runs: <n> of <limit> per stage (slots log)
Review: <requesting-code-review or the code-review skill, research item <key>; findings, or none>
Did not match: <claim against fact, or nothing>
Passport: updated <bead> | nothing new
Answer to the crew: <next stage within the estimate and clearances | fixes | work order closed>
```

A stage another crew or the customer did is closed with `journal.py stage-close` before `crew.py stop`.

## Stage closed without acceptance

Written when a stage cannot be accepted because someone else did its work (another crew, the customer, a merge). Put the filled block into a file and record it with `journal.py stage-close STAGE --act-file FILE`: it records the block as the stage's acceptance act and closes the stage, which need not have been started. The first line must stay as it is. Then `crew.py stop` closes the work order once no stage and no gate is open.

```
[stage closed without acceptance]
Stage: <stage id> <title>; work order <id>
Who did the work: <the customer, another crew, which work order>
Where: <the commit or merge, with the SHA and branch>
Evidence checked: <command and output, files read>
```

## Handover act

The crew writes it as its last message, at the foreman's request (section Handover request), and ends it with the line HANDOVER READY. `crew.py complete_handover` saves it as a `handover` act on the work order without the marker line and appends two blocks the crew does not write: "Facts at handover" (`git status --short`, the branch's commits since the base, the open stages' statuses) and `Spend:` (`journal.spend_block`).

```
Goal: <the work order's goal in one or two sentences>
Done: <commits: SHA and subject; stages finished>
In progress: <background tasks: what runs, where its output is, how to check it; or nothing>
Next steps: <in order>
Agreements: <estimate, clearances, constraints, answers the foreman gave>
Important paths: <files, logs, data>
Open questions: <or none>
HANDOVER READY
```

## Foreman's handover act

Written by the foreman when its own context reaches the threshold. Put the filled block into a file and run `shift.py handoff CALLER_PANE --act-file FILE`: the script saves it as a `shift` act on a new shift bead, appends the `Spend:` block (`journal.spend_block`: the shift's dollars against the budget, the rates) and starts the new foreman in a new pane. The figures come from `crew.py spend WORK_ORDER`, `journal.py shift-spend` and `limits.py rate`.

```
Work orders: <id - title - state (issued, waiting for acceptance, a clearance or the customer, accepted) - current stage - next step>
Crews and panes: <crew name - pane - work order - session id - status>
Spend against estimates: <each work order's dollars of its estimate; research items; the shift's dollars of the budget and pp of the week>
Limits: <5h window % and its reset; week % or unknown; the source; the current rate and how it was obtained>
Expected events: <stop points due, heavy runs in progress, limit resets, gates the customer has to close, the timers of promised times (journal.py due list)>
Night shift: <on or off; morning time; 5h thresholds in force; caffeinate running>
Open questions: <for the customer, or none>
```

## Incident

The foreman logs an incident as an `answer` act on the work order or stage whose text starts with this tag: `journal.py act BEAD answer "[incident] <what happened, which crew, what the foreman did>"`. The morning report lists every such act under Incidents.

```
[incident] <what happened, which crew, what the foreman did>
```

## Customer decision to drop the branch

The foreman writes this `answer` act only after the customer's explicit words dropping the work order's branch. Only a line that starts with this marker lets `cleanup` delete a branch with unmerged commits.

```
[customer decision] drop
```

## Customer approval

The foreman writes this `answer` act when the customer answers its recommendation with words that hand the decision over, in any language. Record it on the work order or stage the recommendation concerns (`journal.py act BEAD answer TEXT`); a recommendation that concerns no bead (the shift's budget, a night shift) goes to kv `approval.<YYYYmmdd-HHMM>` with the same text (`journal.py kv set`). It approves exactly the figure or option you last stated - an estimate, a budget figure, a choice - and nothing beyond it; it lifts no stop: it is the customer setting that figure in words, the stop then applies at it, and the command 'ignore limits and finish' stays the only lift. It is never an open-ended "no limit" (that stays the command 'ignore limits and finish'), never a safety rule, never machine protection. The customer's words stay in the `words:` field of this act only: the morning report, handover acts, gates, push notifications and work orders say 'approved by delegation: <the recommendation>' in English and never copy them.

```
[customer decision] approve: <the recommendation as you last stated it, with its figure, in English>; words: "<the customer's words, verbatim, in the language they wrote>"
```

## Handover request

```
Finish the current step. Do not start new long runs. Write a handover act with the fields: the work order's goal; done (commits); in progress (background tasks, where their output is, how to check it); next steps; agreements (estimate, clearances, constraints); important paths; open questions. When the act is written, answer with the line HANDOVER READY.
```

## Pause

```
Finish the current step, start nothing new, wait for the foreman's message.
```

## Emergency pause

```
The customer's emergency stop is on: make no tool call and do not retry any step that was denied. End your turn now and wait for the foreman's message.
```

## Continue

```
Continue from where you stopped.
```

## Changed work order

Sent by `crew.py restart` after a `--resume` when the work order differs from the one the crew was last given; the work-order file is rewritten first.

```
The work order changed since you last read it: read the file {path}, then continue from where you stopped.
```

## End background shells

Sent by `crew.py stop` when the crew still lists background shells at the close, or `/exit` met "Background work is running", so the script sends no `/exit` (it would offer to stop them). The crew ends its own shells; the foreman waits for the crew's next `settled` for at most one monitor heartbeat, then runs `stop` again. If shells are still listed then, the foreman reads `crew.py summary NAME`, records an incident and gates the work order. A crew settled with `shells:<n>` is accepted as usual; its handover waits for the shells.

```
The foreman is closing this work order: stop every background shell you started, start nothing new, and end your turn; if one cannot be stopped, say which in one line.
```

## Message by file

```
Read the file {path} and do what it says.
```

## Fix the report

```
The stage report failed the check; the mismatches are listed in the file {path}. Fix the report or the work and send the report again.
```
