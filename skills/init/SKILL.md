---
name: init
description: Set up code-guardrails in the current project. Creates guardrails.json and shield.json for the detected stack (Flutter/Dart, Python, Node, Swift) and installs the code shield's git hooks. Use when the user asks to set up, enable, install or initialize the guardrails, the guard hooks or the code shield in a project.
---

# Set up the guardrails in this project

The plugin's hooks do nothing in a project until it has a `guardrails.json`. This skill creates it.

1. From the project root, run `guardrails-init` in Bash. It detects the stack, writes a starter
   `guardrails.json` and `shield.json`, and installs the code shield's git hooks (`pre-commit` and
   `reference-transaction`; an existing hook is backed up, never overwritten). It never overwrites an
   existing `guardrails.json` or `shield.json`. Add `--no-git-hooks` if the user doesn't want git hooks.
2. Read the new `guardrails.json` and check it against the project:
   - `gates`: does each test command match how this project really runs its tests (look at CI config,
     Makefile, package scripts, melos/workspace setup)? Monorepos usually need one gate per package with
     its own `on` globs and a `cd <package> && ...` command.
   - `generated.paths`: every code-generator output (build_runner, protobuf, l10n, OpenAPI clients).
   - `roles`: where tests and production code really live.
   Propose the changes to the user before making them. From now on, edits to `guardrails.json` ask the
   user for approval: that is the guard protecting its own setup, not an error.
3. Run `guardrails-check`. The last line must be `ok`; fix every `PROBLEM:` line it prints.
4. Tell the user, briefly:
   - The hooks are active from the next prompt.
   - To protect critical code (money rules, encoders, deterministic math), use the plugin's lock skill
     (`/code-guardrails:lock`).
   - The role agents (`code-guardrails:card-auditor`, `test-writer`, `implementer`, `reviewer`) are
     available; their write boundaries come from `roles` in `guardrails.json`.

If `guardrails-init` is not found, the plugin's `bin/` folder isn't on PATH in this session: ask the user
to restart Claude Code after installing the plugin.
