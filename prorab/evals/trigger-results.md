# Trigger evals: results

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
