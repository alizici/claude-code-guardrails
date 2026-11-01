---
name: lock
description: Lock a piece of critical code with the code shield so it can't change without the user's approval and passing tests. Use when the user asks to lock, protect, freeze or shield a function, rule, file or region (pricing and money rules, protocol encoders/decoders, deterministic math, security checks).
---

# Lock critical code with the code shield

A lock is a file, or a region between `SHIELD: <name>` and `SHIELD-END: <name>` markers, plus the tests
that protect it. Once approved, any change to it stops the turn and the commit until the user approves
the change with a reason, and the change isn't trusted until its tests pass.

1. **Pick the scope.** The smallest region that holds the rule. Name it in kebab-case (`fee-rule`).
2. **Find its tests.** A lock without tests can't be verified. If there are no tests that would fail when
   the rule changes, say so and offer to write them first (the `code-guardrails:test-writer` agent).
3. **Mark the region** in the file's own comment style, on lines of their own:
   ```
   // SHIELD: fee-rule
   ...the rule...
   // SHIELD-END: fee-rule
   ```
   (`#`, `--`, `;`, `<!-- -->` and `/* */` work too.) Skip the markers to lock a whole file.
4. **Add the lock to `shield.json`** with an empty fingerprint. Editing `shield.json` asks the user: that's
   expected.
   ```json
   {"name": "fee-rule", "file": "lib/billing/fees.dart", "region": "fee-rule",
    "reason": "What breaks, and for whom, if this changes silently.",
    "tests": {"dart": ["test/billing/fees_test.dart"]}, "fingerprint": ""}
   ```
   The key under `tests` is a runner from `runners`; `{tests}` in its command is replaced by the names.
5. **Verify and approve:** `guardrails-shield test fee-rule`, then
   `guardrails-shield approve fee-rule --reason "<what is locked and why>"`. Approval asks the user and is
   written to `docs/SHIELD_LOG.md`. Never write a fingerprint by hand.
6. Finish with `guardrails-shield list`: the lock should read `verified`.

If the scripts were copied into the project instead of installed as a plugin, the commands are
`python3 guardrails/shield.py test|approve|list`.
