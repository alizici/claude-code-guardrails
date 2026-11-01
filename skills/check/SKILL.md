---
name: check
description: Check that code-guardrails is set up correctly in the current project and explain what it would do. Validates guardrails.json, runs the deletion guard's self-test, lists the code shield's locks, and answers "would the guard allow writing this file / running this command?". Use when the user asks whether the guardrails work, why something was blocked, or what a rule would do.
---

# Check the guardrails

Run these from the project root, in Bash, and report the result in a few lines:

1. `guardrails-check` validates `guardrails.json`: every block/ask rule against its own examples, every
   gate's tool, and the hook state under concurrency. The last line must be `ok`; otherwise list each
   `PROBLEM:` line with the fix.
2. `guardrails-deletion selftest`: the deletion guard end to end in a throwaway repository. Last line `ok`.
3. `guardrails-shield list`: each lock and its status (`verified`, `untested`, `CHANGED`, `DELETED`). A
   `CHANGED` lock needs either a revert or `guardrails-shield test <name>` and then the user's approval.

When the user asks what the guard would do, ask it directly instead of guessing:

- `guardrails-check --file <path>`: is writing this file allowed, and which gates does it trigger?
- `guardrails-check --command '<shell command>'`: is this command allowed, asked or denied?
- Add `--role test-writer` (or another role) to decide as that subagent.

Lost a line or a file? The deletion guard keeps a numbered version of the working tree for every turn:
`guardrails-deletion history [path]`, `guardrails-deletion show <N> <path>`,
`guardrails-deletion restore <N> <path>`.

If `guardrails-check` says there is no `guardrails.json`, the project isn't set up: offer the init skill
(`/code-guardrails:init`).
