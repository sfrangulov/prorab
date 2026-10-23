# Trigger evals: results

## Command-only (2026-10-23, R121)

The customer's order: the skill starts by its command, not by words (it loaded in ordinary sessions). The frontmatter holds `disable-model-invocation: true` and `argument-hint: "[goal text or goal file]"`; the `description` is one plain sentence with no trigger phrases. With the flag Claude Code keeps the skill out of the model's skill list (its description is not in the model's context), so no query can make the model invoke it; `/prorab` loads it, with any text after it as its arguments.

Expected now, for the same 32 queries of `triggers.json`: the 30 phrase queries, English and Russian, the 14 former should-trigger phrases among them, invoke nothing; the two `/prorab` queries (`en-t4`, `ru-t4`) invoke the skill through the command.

- Checked here: `test_references.CommandOnlyTests` pins the frontmatter (the flag, the hint, a one-sentence `description` with no quotes, Cyrillic or trigger wording) and these expectations; the Cyrillic guard no longer exempts the `description`.
- Not rerun with subagents: the model never sees the skill, so a subagent choosing from a skill list would test the list handed to it, not Claude Code.
- **Live check, open:** in a fresh Claude Code session in the office, the skill list shows no `prorab`; the queries `en-t1` and `ru-t1` load nothing; `/prorab` loads it, and so does the handover's `/prorab take over the shift from bead <id>.` sent by `shift.py handoff`.

## Description triggers (2026-10-22, superseded by R121)

Run 2026-10-22 against the `description` of SKILL.md (round-1 frontmatter, unchanged in REFACTOR round 1). One fresh Sonnet 5.5 subagent per query. Each got the same instructions: it stands in for a Claude Code session in an ordinary project directory with no project instructions, reads a list of 52 skills (this machine's installed skills plus the harness built-ins, names and descriptions verbatim, prorab in alphabetical position), and names the skill it invokes first, or `none`. A query passes when `prorab` is chosen exactly for the should-trigger queries.

| Query | Expected | Chosen | Pass |
|---|---|---|---|
| en-t1 | prorab | prorab | yes |
| en-t2 | prorab | prorab | yes |
| en-t3 | prorab | prorab | yes |
| en-t4 | prorab | prorab | yes |
| en-t5 | prorab | prorab | yes |
| en-t6 | prorab | prorab | yes |
| en-t7 | prorab | prorab | yes |
| en-t8 | prorab | prorab | yes |
| en-n1 | not prorab | superpowers:systematic-debugging | yes |
| en-n2 | not prorab | superpowers:test-driven-development | yes |
| en-n3 | not prorab | code-review | yes |
| en-n4 | not prorab | superpowers:brainstorming | yes |
| en-n5 | not prorab | superpowers:subagent-driven-development | yes |
| en-n6 | not prorab | loop | yes |
| en-n7 | not prorab | superpowers:dispatching-parallel-agents | yes |
| en-n8 | not prorab | schedule | yes |
| ru-t1 | prorab | prorab | yes |
| ru-t2 | prorab | prorab | yes |
| ru-t3 | prorab | prorab | yes |
| ru-t4 | prorab | prorab | yes |
| ru-t5 | prorab | prorab | yes |
| ru-t6 | prorab | prorab | yes |
| ru-t7 | prorab | prorab | yes |
| ru-t8 | prorab | prorab | yes |
| ru-n1 | not prorab | superpowers:systematic-debugging | yes |
| ru-n2 | not prorab | superpowers:test-driven-development | yes |
| ru-n3 | not prorab | code-review | yes |
| ru-n4 | not prorab | superpowers:brainstorming | yes |
| ru-n5 | not prorab | superpowers:subagent-driven-development | yes |
| ru-n6 | not prorab | loop | yes |
| ru-n7 | not prorab | superpowers:dispatching-parallel-agents | yes |
| ru-n8 | not prorab | schedule | yes |

**32 of 32:** English 16/16, Russian 16/16; should-trigger 16/16, should-not-trigger 16/16. The queries are in `triggers.json` under the same ids.
