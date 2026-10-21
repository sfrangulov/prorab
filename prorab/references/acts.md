# Act texts and messages to crews

Scripts take the first code block under a section heading. Do not rename the headings.

## Handover request

```
Finish the current step. Do not start new long runs. Write a handover act with the fields: the work order's goal; done (commits); in progress (background tasks, where their output is, how to check it); next steps; agreements (estimate, clearances, constraints); important paths; open questions. When the act is written, answer with the line HANDOVER READY.
```

## Pause

```
Finish the current step, start nothing new, wait for the foreman's message.
```

## Customer decision to drop the branch

The foreman writes this `answer` act only after the customer's explicit words dropping the work order's branch. Only a line that starts with this marker lets `cleanup` delete a branch with unmerged commits.

```
[customer decision] drop
```

## Message by file

```
Read the file {path} and do what it says.
```

## Continue

```
Continue from where you stopped.
```

## Fix the report

```
The stage report failed the check; the mismatches are listed in the file {path}. Fix the report or the work and send the report again.
```
