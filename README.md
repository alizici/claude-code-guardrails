# Claude Code Guardrails

**Hooks, role agents and a code shield that let you use Claude Code on a real codebase every day, fast, without the AI quietly breaking what already worked.**

AI coding agents are fast. The problem isn't speed, it's the silent damage: a guard clause that disappears during a "cleanup", a test that gets weakened until it passes, a generated file edited by hand, a critical rule rewritten while the agent was fixing something else. You usually find out weeks later.

This repository is the guardrail system I built and use daily on my own projects (a Flutter/Dart monorepo and a real-time multiplayer game with a .NET server). It's extracted, made language-agnostic and documented here, and it installs as a Claude Code plugin.

```
/plugin install code-guardrails --marketplace alizici/claude-code-guardrails
/code-guardrails:init
```

```
                    ┌──────────────────────────────────────────────────────────────┐
  every tool call → │ guard.py protect     generated files · protected paths ·    │ → deny / ask / allow
                    │                      blocked commands · role boundaries      │
                    │ deletion_guard.py    important lines can't vanish undeclared │
                    └──────────────────────────────────────────────────────────────┘
                    ┌──────────────────────────────────────────────────────────────┐
  end of turn     → │ guard.py gates       tests/checks triggered by changed files │ → the turn can't end
                    │ deletion_guard.py    diff vs. turn start (Bash edits too)    │   while something is red
                    └──────────────────────────────────────────────────────────────┘
                    ┌──────────────────────────────────────────────────────────────┐
  every commit    → │ shield.py            locked critical code needs approval     │ → commit / cherry-pick
                    │ (git hooks)          + its tests, with a reason in a log     │   stopped
                    └──────────────────────────────────────────────────────────────┘
```

## What's inside

### 1. Guard hook · [`guardrails/guard.py`](guardrails/guard.py)

Runs on every tool call and at the end of every turn.

- **Generated files are read-only.** By path glob or by a generator stamp in the first 30 lines (`GENERATED`, `DO NOT EDIT`, `@generated`…). The agent is told how to regenerate instead.
- **Protected paths ask the user.** E.g. the lock list, role definitions, secrets. The guard's own setup (hook settings, config, scripts) is protected too, so the agent can't loosen the guard to get past it.
- **Blocked and ask-first commands.** Force push, `--no-verify`, `reset --hard`… each rule carries its own `match` / `no_match` examples, and `guard.py check` verifies them (a real bug this caught: a force-push pattern also matched `git worktree remove --force` later on the same line).
- **Shell commands are parsed for write targets.** Redirects, `cp`/`mv`/`rm`/`tee`/`sed -i`, `git checkout --`, formatters with `--write`, and `python -c`/`node -e` one-liners that open a file for writing. Quotes and heredocs are handled, so `echo 'a > b'` isn't a write.
- **Role boundaries for subagents.** A test writer can only write tests, an implementer can't touch tests, an auditor or reviewer can't write in the project at all. Roles are config, not code.
- **End-of-turn gates.** Each gate has trigger globs and a command. Files changed in the turn (by any tool, found via a git snapshot taken at the start of the turn) decide which gates run. A failing gate blocks the end of the turn with the failure output. The agent is told not to weaken the test to pass. Loop protection: it only blocks again when the working tree changed since the last block, and at most `max_blocks` times.
- **Never blocks on its own failure.** A missing tool, a timeout or a broken config is a warning, never a block. The script never exits with code 2 (Claude Code reads 2 as "block").

### 2. Deletion guard · [`guardrails/deletion_guard.py`](guardrails/deletion_guard.py)

Line-level protection against accidental deletions, plus a numbered version of the working tree on every turn.

- Before an edit, it computes exactly which lines will be deleted. Dropping an **important line** (a class, function, test, guard clause, `throw`/`assert`/`raise`, a YAML/JSON key, any line of a doc) without declaring it first is denied. So is rewriting an existing file with `Write` while dropping lines.
- A line replaced by a similar line counts as changed, not deleted. A line that moved (within the file, or to another file in the same turn) isn't a deletion.
- At the end of the turn it diffs against the turn-start snapshot, so deletions done through Bash or scripts count too.
- Every turn is a dated, numbered version stored under `refs/guardrails/…`, using a temporary index: your branch, HEAD and staging area are never touched. `history`, `show <N> <file>` and `restore <N> <file>` bring anything back.

### 3. Code shield · [`guardrails/shield.py`](guardrails/shield.py)

For the code that must never change silently: money rules, protocol encoders, deterministic math.

- A lock is a file, or a region between `SHIELD:` / `SHIELD-END:` markers in any comment style, plus the tests that protect it and an approved fingerprint (whitespace-insensitive).
- Any change needs `shield.py approve <lock> --reason "…"`, which the guard sends to the user for approval, and which is written to [`docs/SHIELD_LOG.md`](docs/SHIELD_LOG.md).
- An approved change isn't trusted until its lock tests pass: the end-of-turn gate runs them for every unverified lock.
- Two git hooks: `pre-commit` and `reference-transaction`. The second one exists because cherry-pick, revert, rebase, am, merge and `commit -n` skip `pre-commit`. In practice a cherry-pick once committed an unapproved lock change; this closes that path.

### 4. Role agents · [`agents/`](agents/)

A card pipeline where no agent grades its own work:

| Role | Does | Can write |
|---|---|---|
| [`card-auditor`](agents/card-auditor.md) | Checks the task card before coding: missing decisions, invented expected values, uncovered branches | nothing (report only) |
| [`test-writer`](agents/test-writer.md) | Writes the acceptance tests first and shows them **red** | tests only (+ empty stubs) |
| [`implementer`](agents/implementer.md) | Smallest correct code that turns them green; writes open questions down instead of guessing | production code only |
| [`reviewer`](agents/reviewer.md) | Clean context, never sees the implementer's reasoning; measures test strength with mutants | nothing (report only) |

The boundaries in the right column aren't instructions the model may forget: they're enforced by the guard hook.

## Install as a Claude Code plugin

Requirements: Python 3.9+, git, Claude Code 2.1.275 or later for the one-step install.

In Claude Code:

```
/plugin install code-guardrails --marketplace alizici/claude-code-guardrails
```

or in two steps, which also works on older versions and from a terminal:

```bash
claude plugin marketplace add alizici/claude-code-guardrails
claude plugin install code-guardrails@alizici
```

Then, in each project you want guarded:

```
/code-guardrails:init
```

`init` detects the stack (Flutter/Dart, Python, Node, Swift), writes a starter `guardrails.json` and `shield.json`, and installs the shield's git hooks. Review the gates it proposes against how your project really runs its tests.

**The plugin is opt-in per project.** Its hooks do nothing in a project without a `guardrails.json`, so installing it at user scope is safe: other projects aren't touched, and no snapshots are taken there.

What the plugin adds:

| Part | What it is |
|---|---|
| hooks | the guard, the deletion guard and the end-of-turn gates ([`hooks/hooks.json`](hooks/hooks.json)) |
| agents | `code-guardrails:card-auditor`, `test-writer`, `implementer`, `reviewer`; their write boundaries come from `roles` in your `guardrails.json` |
| skills | `/code-guardrails:init`, `/code-guardrails:check` (is it set up right, what would the guard do), `/code-guardrails:lock` (put critical code under the shield) |
| commands | `guardrails-check`, `guardrails-shield`, `guardrails-deletion`, `guardrails-init`, on the Bash tool's PATH inside Claude Code |

Ask the guard what it would do:

```bash
guardrails-check --file lib/model.g.dart
guardrails-check --command 'echo x > src/pricing.py' --role test-writer
guardrails-shield list
```

Updates: third-party marketplaces don't auto-update by default. Run `/plugin marketplace update alizici`, or turn on auto-update for it under `/plugin` → Marketplaces.

A gate that needs the shield calls it through `$GUARDRAILS_BIN`, the scripts' folder wherever the plugin is installed: `"run": "python3 \"$GUARDRAILS_BIN/shield.py\" gate"`.

## Or copy it into the project

No plugin, everything in the repository (useful when the whole team must get the guard from git):

```bash
# 1. copy into your project
cp -r guardrails/ /path/to/project/
cp guardrails.json shield.json /path/to/project/
mkdir -p /path/to/project/.claude
cp .claude/settings.json /path/to/project/.claude/   # hook wiring (merge if you already have settings)
cp -r agents/ /path/to/project/.claude/agents/       # role agents

# 2. adapt guardrails.json (generated paths, gates, roles) and shield.json (runners, locks), then validate
cd /path/to/project
python3 guardrails/guard.py check                    # last line must be "ok"
python3 guardrails/deletion_guard.py selftest        # last line must be "ok"

# 3. install the shield's git hooks
sh guardrails/install_git_hooks.sh
```

Use one or the other. If a project has its own copy wired into `.claude/settings.json`, the plugin's hooks step aside there, so nothing is checked twice.

## Try it on the demo

[`examples/demo/`](examples/demo/) is a tiny pricing module with one locked rule (`discount-rule`) and its tests.

```bash
python3 guardrails/shield.py test                 # runs the lock's tests and marks it verified
python3 guardrails/shield.py list                 # verified   discount-rule   examples/demo/src/pricing.py [discount-rule]
perl -pi -e 's/min\(loyalty_years, 10\)/min(loyalty_years, 20)/' examples/demo/src/pricing.py
python3 guardrails/shield.py check                # CHANGED  discount-rule … → exit 1, and a commit is refused
git checkout examples/demo/src/pricing.py
```

## Configuration

[`guardrails.json`](guardrails.json) (guard + deletion guard):

| Key | Meaning |
|---|---|
| `generated.paths`, `generated.stamp_patterns`, `generated.regenerate` | read-only files and how to regenerate them |
| `block`, `ask` | regex rules for shell commands, each with `reason` and `examples.match` / `examples.no_match` |
| `ask_paths` | globs whose writes need the user's approval |
| `roles.<agent-name>.write` | globs a subagent of that type may write (empty = read-only) |
| `gates[]` | `name`, `on` (trigger globs), `run` (string or argv list), `timeout` |
| `max_blocks`, `infra_exit_codes`, `extra_paths`, `protect_setup`, `setup_decision` | tuning |
| `deletion_guard.*` | `watched`, `excluded`, `all_lines_important`, `allowed_deletions`, `rewrite`, `important_deletion` |

[`shield.json`](shield.json): `runners` (a command with a `{tests}` placeholder) and `locks` (`name`, `file`, optional `region`, `reason`, `tests`, `fingerprint`).

## Tests

```bash
python3 -m unittest discover -s tests
```

50 tests, each against a throwaway git repository: protection decisions, shell parsing, role boundaries, gate blocking and loop protection, the shield's pre-commit **and** cherry-pick paths, deletion detection, moves vs. deletions, restore, and the plugin install (the hooks run with the exact commands from `hooks/hooks.json`, against a project that only has the config files). Each guard was also checked by breaking it on purpose and watching its test go red. The guard's `check` command additionally runs a concurrency test: 32 parallel hook processes writing the same session state, a real failure mode once 10–20 subagents run at the same time.

## Design notes

- **A guardrail, not a security boundary.** An agent that writes a script and runs it can't be stopped up front. That's what the end-of-turn gates (triggered by changed files, whatever changed them) and CI are for.
- **Fail open on infrastructure, fail closed on findings.** A broken hook must never stop your work; a red test must never be skipped.
- **Every rule earned its place.** Most rules here exist because the failure they prevent actually happened: a revert script that silently deleted a decoder function, a cherry-pick that slipped past the commit hook, parallel subagents overwriting each other's state, a test writer that ignored the card's test names.
- **Measure, don't assume.** In my own setup every subagent run is logged, and a change to the guardrails stays only if the numbers improve.

## License

MIT, see [LICENSE](LICENSE).

---

Built by **Muhammed Ali Çınar**, senior Flutter & iOS developer.
