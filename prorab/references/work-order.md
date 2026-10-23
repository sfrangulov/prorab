# Work order

The foreman writes every work order from the template below, for a crew that knows nothing about the task.

1. Fill in every `<...>`; write "none" rather than drop a field.
2. Create the work order: `journal.py work-order SITE TITLE --body TEXT --estimate-usd USD [--heavy-runs N] [--goal GOAL]` with the filled text as the body; it returns the id that replaces `<id>` in the text sent to the crew. `crew.py check_report` takes the acceptance criteria from this body: every line that starts with a C-id (`C1:`, `- C1:`, `| C1 |`) is a criterion the report must answer, so only the criteria list starts lines with one.
3. Create each stage with its Files and Expected duration, min: `journal.py stage WORK_ORDER TITLE --file PATH [--file PATH ...] --duration MIN [--blocked-by STAGE]`. The work order's files are the union of its stages' files: `crew.py start` refuses a crew on the same site whose files overlap, and the report check rejects a change outside them. A path ending in `/` covers everything below it; any other entry is a shell pattern (`fnmatch`).
4. When a stage begins: `journal.py stage-start STAGE`; its heavy runs are counted from that time.
5. Issue: append the site passport's text (`journal.py passport SITE`) below the filled template — and, without the superpowers plugin, the fallback procedure of `superpowers.md` — and send it with `crew.py send_work_order NAME TEXT --work-order ID`. The text goes into `~/.cache/prorab/work-orders/<id>.md`; the crew gets one line naming the file.
6. At every stop point: `crew.py check_report NAME WORK_ORDER --stage STAGE`, always with `--stage`, before `crew.py accept` (`acts.md`, Acceptance act). Without `--stage`, or for a stage with no start time, the report is rejected as "heavy runs not verifiable".
7. When the check fails, send the problems back, one per line in a file: `crew.py report_fix NAME WORK_ORDER STAGE --problems-file FILE`. It counts the rounds against `cycles.report_fix` in `defaults.json`; once they are used up it sends nothing and raises a gate instead, and the stage waits for the customer.

## Template

````markdown
# Work order <id>: <title>

## Site
- Path: <site path>. You work in its worktree `prorab-<id>` on the branch `prorab/<id>` (`git branch --show-current`); the base branch is `<base branch>`.
- Crew: Claude.

## Goal and why it matters
<What is to be achieved and why, for a reader who knows nothing about the task: the problem, who needs the result, the decisions already taken, the design document if there is one.>

## Stages
Every stage ends at its stop point: commit, leave no background command running, send the stage report (section Report format) as your last message and wait for the foreman's answer. Never start the next stage on your own.

### Stage 1: <title>
- Work: <what to do>
- Files: <the paths this stage changes>
- Expected duration, min: <minutes>
- Stop point: <the state at which you stop and report>

### Stage 2: <title>
- <the same fields>

## Constraints
- Work only in this worktree, on the branch `prorab/<id>`, and commit there. Never push, open a pull request, release, or merge into main or any other branch.
- While the customer has enabled crew hooks, a hook denies the commands the foreman's rules forbid and, during an emergency stop, every tool call; a denied step is a question for the foreman, never a reason to find another way.
- Change only the work order's files: the union of the stages' Files (a path ending in `/` covers everything below it). A change outside them fails the report check; ask the foreman first if you need another file.
- Do not touch installed things: installed skills and plugins, user configs (`~/.claude`, shell profiles), global packages; no `sudo`.
- Never answer a trust prompt or any other consent prompt, and never press "Upgrade": stop and tell the foreman.
- Never run `/login`, `claude auth`, `claude login`, `claude logout` or `claude setup-token`, and never answer a login screen: logging in is the customer's. If a command says Claude Code is not logged in, stop and tell the foreman in one line.
- This work order has one crew and one session at a time: start no other Claude session for it (a `claude -p` run inside this worktree counts in the estimate), and take no second work order.
- Safety rules: no `nohup` or detached runs; do not read credential stores, and put no secrets into files or messages; mass runs, heavy downloads and anything the machine's endpoint security agents would notice only with a clearance (section Clearances). Keep the site's beads as the site's `CLAUDE.md` says; the foreman's journal is not yours to write. If the site has no .beads of its own, run no bd here: bd would walk up to a parent folder's beads database, which belongs to another project. Never use a parent folder's beads database.
- Heavy commands (LibreOffice, test suites, builds) run from inside this worktree through `<absolute path>/prorab/scripts/slots.py run --kind heavy -- <command>`. Each run counts against the heavy-run limit of the stage; at the limit, stop and ask.
- Small independent tasks inside this work order go to your own subagents (`dispatching-parallel-agents`, `subagent-driven-development`); their spend counts in the estimate.
- The foreman is your human partner in the superpowers process: superpowers questions and approval requests go to the foreman. Ask them as plain text when you can; a choice dialog is answered too, but plain text is cheaper to read.

## Estimate
- Estimate: $<usd> at API prices for the whole work order: every session, subagent and `claude -p` run in this worktree. The foreman computes the spend from the transcripts; you do not report it. If the work will not fit, say so at the stop point.
- Heavy-run limit per stage: <n>. More needs a clearance.

## Clearances
<What a clearance already allows, by stage (for example "stage 2: up to 40 heavy runs", "stage 1: download the test data set, 2 GB"), or none. Whether the customer gave the foreman the right to close clearance gates.>

## Site passport
Bead <passport id>. Its text follows this work order: worktree setup, quick checks, heavy commands, known quirks, actions that need a clearance.

## Acceptance criteria
The foreman checks these facts at acceptance:
- C1: <a checkable fact: a command and its expected output, a file and its content, a commit>
- C2: <...>
- Cn: <...>

## Report format
At every stop point your last message is the stage report, as plain text in the chat: the foreman reads it from the transcript and checks it with a script. Use this form:

```
# Stage <n> report: <title>

Result: <delivered | no changes | blocked | abandoned>
Commit: <SHA of the stage's last commit, or none>
Heavy runs: <n>

## Summary
<two or three sentences: what was done>

## Behaviour
<what the code or document now does differently>

| C-id | status | reason |
|---|---|---|
| C<n> | <status> | <reason> |

## Changed files
- `<path>` - <what changed>

## Verification
- `<exact command>` - <the line of its output that shows the result> - SKIP: <n>

## Residual risks
<what may still be wrong, or none>
```

- Five sections with these headings: Summary, Behaviour, Changed files, Verification, Residual risks.
- One table row per acceptance criterion. The status is done, not applicable, deferred or blocked; every status but done needs a reason.
- Result: delivered, no changes, blocked or abandoned. With delivered, `Commit:` is a commit on `prorab/<id>` made since the base, Verification holds at least one check command, and Changed files lists exactly the files of `git diff --name-only <base branch>...HEAD` (the whole branch, not only this stage), one path in backticks per item.
- Heavy runs: the heavy runs of this stage through `slots.py`, 0 if none. The check compares the number with the log of `slots.py`; a report without the line, or with another number, is rejected.
- Every item under Verification is one check command in backticks, with its output and `SKIP: <n>`, the number of skipped tests (0 if none). "Tests are green" without a command is not accepted; remarks go under Residual risks.
- No spend in dollars: the foreman computes it.
````

## Example stage report

A report that passes `crew.py check_report` once its commit is on the crew's branch (the work order has the criteria C1, C2 and C3; its files cover both changed paths).

```
# Stage 1 report: CSV export

Result: delivered
Commit: 4e1d2c7
Heavy runs: 1

## Summary
Added a CSV export of the daily report.

## Behaviour
`demo-app report --csv` prints the daily report as CSV with a header row; without the flag the output is unchanged.

| C-id | status | reason |
|---|---|---|
| C1 | done | |
| C2 | done | |
| C3 | deferred | archived reports are stage 2 |

## Changed files
- `src/demo_app/report.py` - the CSV writer and the `--csv` flag
- `tests/test_report.py` - tests of the CSV output

## Verification
- `/home/user/projects/prorab/prorab/scripts/slots.py run --kind heavy -- python3 -m unittest discover -s tests` - `Ran 14 tests ... OK` - SKIP: 0

## Residual risks
The CSV has not been opened in a spreadsheet application.
```
