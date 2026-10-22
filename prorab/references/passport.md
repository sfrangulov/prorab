# Site passport

A site passport tells a crew how to work on a site: a new worktree lacks whatever git does not hold. It is a bead in the foreman's office journal with the labels `site:<name>` and `prorab:passport`; it lives in the journal only, never in the site and never in the skill.

- Write: fill in the template into a file and run `journal.py passport SITE --set-file FILE`; the command creates the bead or replaces its text.
- Read: `journal.py passport SITE` prints `{"id", "text"}`. The text goes below the work order in the work-order file (`work-order.md`, step 5), and the work order names the bead.
- Update: after acceptance, whatever the stage taught (a missing setup step, a new quirk, a heavy command) goes into the passport at once.

## Template

```
# Site passport: <site name>

Path: <site path>

## Worktree setup
<Commands to run in a new worktree `prorab-<id>`, in order: what git does not hold (.venv, node_modules, .env and where it comes from, local data). Heavy downloads only with a clearance.>

## Quick checks
<Cheap commands that need no heavy slot and no clearance, each with its expected result.>

## Heavy commands
<Test suites, builds, LibreOffice: each as `<absolute path>/prorab/scripts/slots.py run --kind heavy -- <command>`, run from inside the worktree, with its usual duration.>

## Known quirks
<What surprises a newcomer: flaky tests, slow steps, generated files, tools the site expects.>

## Actions that need a clearance
<Mass runs, heavy downloads, anything the machine's endpoint security agents would notice, and the site's own risky actions.>
```
