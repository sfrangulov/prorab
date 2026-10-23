# Crew process: superpowers

Crews follow the superpowers process. Its skills turn to the "human partner" with questions and approval requests; for a crew that partner is the foreman, which answers on the customer's behalf within the work order and the budget. The customer takes part once per goal, at the start.

## Levels

| Level | superpowers skill | Who takes part |
|---|---|---|
| Goal | `brainstorming` | the customer and the foreman, once per goal and briefly: the customer can say "go ahead". The result is a design document or a description in the work order |
| Plan | `writing-plans` | the foreman. The plan's tasks become work orders and stages in the journal |
| Execution | `executing-plans` or `subagent-driven-development`, inside them `test-driven-development`, `systematic-debugging`, `verification-before-completion` | the crew. The `using-git-worktrees` requirement is already met: the work order has a worktree |
| Acceptance | `requesting-code-review` or the `code-review` skill with a subagent checking the branch diff | the foreman, on top of checking the facts |
| End of branch | `finishing-a-development-branch` | not done at night: the options "merge / PR / keep" go into the morning report, and the customer decides |

## Rules

- **A work order is written so that the crew finds the task clear.** It holds the goal and its meaning, the constraints and the decisions already taken, so `brainstorming` in the crew does not turn into an interrogation. Crews hold no `brainstorming` of their own with the customer.
- **The foreman answers the crews' questions and approval requests,** drawing on the work order, the design document and the journal. `crew.py answer NAME --text TEXT` (or `--choice N` in a choice dialog) sends the answer and records it as an `answer` act on the stage bead: the work order's current open stage, or the work order itself when no stage is open.
- **The foreman approves a design or a plan only by the approval rule** (section Approval); otherwise it returns the work with a list of what to rework.
- **Cycles are limited:** approval and review rounds per stage are counted in the journal against `cycles.approval` and `cycles.review` in `defaults.json`, and report-fix rounds by `crew.py report_fix` against `cycles.report_fix` (`work-order.md`, step 7). When a limit is used up, the customer decides: a gate is raised.
- **A crew's question about a refused command** (Claude Code's worktree guard, whose text starts "This session is isolated in the worktree", or a crew hook) is answered with the plain form of the command (a literal path, one step per call, run from the worktree, the file tools for files) or with a stop for the customer's decision, never with a way around the refusal: no script file, `bash -c`, `eval`, other tool or other folder doing the same. The refusals are counted in the stage report against the transcript.
- **If the foreman cannot answer** (the goal changes, the budget would be exceeded, a matter of taste only the customer can settle), it raises a gate to the customer: `journal.py wait STAGE REASON`. Such questions go into the morning report as one list; meanwhile the crew takes another stage or stops (`crew.py pause NAME`).
- **If superpowers is not installed,** the foreman appends the fallback procedure below to the work-order file.
- **Subagents are not free.** The crew's subagents count in the work order's estimate. The foreman's review subagent at acceptance runs under its own estimate (`journal.py research KEY --estimate-usd USD --session SESSION`); a refusal means no subagent.

## Approval

The foreman approves a design or a plan only when it has no open questions, no placeholders, and enough context to go on without inventing requirements. Otherwise it returns the work with a short list: which sections and which questions need another iteration.

```
Not approved. Rework:
- <section>: <what is missing, unclear or still a placeholder>
- <question>: <what is still open and what would settle it>
```

Before answering, the foreman counts the round:

- an approval request for a design or a plan: `journal.py iterations STAGE approval --bump`;
- a review round, review findings sent back for rework: `journal.py iterations STAGE review --bump`.

The command prints `{"n", "limit", "exceeded"}`; the limit is `cycles.approval` or `cycles.review` from `defaults.json`. When `exceeded` is true, the foreman neither approves nor sends another rework list: it raises a gate to the customer (`journal.py wait STAGE REASON`), and the crew takes another stage or stops.

## Fallback procedure

Without the superpowers plugin the crew follows these steps in every stage; the approval points are the same as with superpowers.

1. **Plan.** Send the foreman a short plan as plain text: the tasks in order, the files each one touches, the test each one starts with, the checks of the acceptance criteria. Wait for the approval; write no code before it.
2. **Test.** For each task, first write a test of the required behaviour, run it and see it fail for the expected reason.
3. **Implementation.** Make the smallest change that makes the test pass, and run it again. When a test fails unexpectedly, find the cause before changing code. Commit each finished task on `prorab/<id>`.
4. **Verification.** Run the checks of the acceptance criteria and the passport's quick checks (heavy ones through `slots.py`) and read their output. Claim nothing that no command of this step showed; keep the commands, their output and the number of skipped tests for the report.
5. **Review.** Read the whole diff `git diff <base branch>...HEAD` against the work order and the plan, fix what you find, then send the stage report at the stop point. Review findings the foreman sends back are a review round: fix them and report again.

The end of the branch (merge, pull request or keep) is never the crew's step: the customer decides.
