# Act texts and messages to crews

`crew.py` sends the first fenced block under the headings Handover request, Pause, Continue, Message by file and Fix the report; the block under Customer decision to drop the branch equals the marker `crew.DROP_MARKER`. Do not rename these headings and keep each block in its own section. The act templates show the form of each act, with every `<...>` to fill in; each section names who writes the act and the command that records it.

## Acceptance act

Written after `crew.py check_report NAME WORK_ORDER --stage STAGE` passed and the claims were compared with the facts. Put the filled block into a file and record it with `crew.py accept WORK_ORDER STAGE --act-file FILE`; `accept` checks nothing itself, appends the `Spend:` block (`journal.spend_block`: the work order's dollars against its estimate, the shift's against the budget, the rates) and closes the stage. Do not write the spend yourself. The first line is the verdict; the morning report shows it.

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
Answer to the crew: <next stage within the estimate and clearances | fixes | work order closed>
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
Expected events: <stop points due, heavy runs in progress, limit resets, gates the customer has to close>
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

## Handover request

```
Finish the current step. Do not start new long runs. Write a handover act with the fields: the work order's goal; done (commits); in progress (background tasks, where their output is, how to check it); next steps; agreements (estimate, clearances, constraints); important paths; open questions. When the act is written, answer with the line HANDOVER READY.
```

## Pause

```
Finish the current step, start nothing new, wait for the foreman's message.
```

## Continue

```
Continue from where you stopped.
```

## Message by file

```
Read the file {path} and do what it says.
```

## Fix the report

```
The stage report failed the check; the mismatches are listed in the file {path}. Fix the report or the work and send the report again.
```
